"""Small random-weight architecture probe; never scientific model evidence."""
import os
from importlib.metadata import version

from ..model_policy import local_code_policy
from .schema import require


def probe(config):
    require(os.environ.get("SLURM_JOB_ID") and not os.environ.get("CUDA_VISIBLE_DEVICES"),
            "architecture probe requires a CPU Slurm allocation")
    require(os.environ.get("HF_HUB_OFFLINE") == "1" and os.environ.get("TRANSFORMERS_OFFLINE") == "1",
            "architecture probe must be offline")
    runtime = config["hf_runtime"]
    for package, required in runtime["runtime_versions"].items():
        require(version(package) == required, "CPU runtime differs: " + package)
    import torch
    import transformers
    from transformers import AutoConfig, AutoModelForCausalLM, GenerationConfig

    model_path = config["model"]["path"]
    reviewed = local_code_policy(model_path, {**runtime, "model_revision": config["model"]["revision"]})
    cfg = AutoConfig.from_pretrained(model_path, local_files_only=True, trust_remote_code=reviewed)
    # Only this in-memory synthetic config is reduced. The original snapshot,
    # formal configuration and tokenizer are never edited or saved over.
    for field, value in {"vocab_size":256, "hidden_size":64, "intermediate_size":128,
                         "num_hidden_layers":2, "num_attention_heads":4, "num_key_value_heads":2,
                         "head_dim":16, "pad_token_id":0, "bos_token_id":1, "eos_token_id":2}.items():
        setattr(cfg, field, value)
    torch.set_num_threads(min(runtime["cpu_threads"], 4))
    torch.manual_seed(2026100801)
    with torch.device("cpu"):
        model = AutoModelForCausalLM.from_config(cfg, trust_remote_code=reviewed,
            torch_dtype=torch.float32, attn_implementation=runtime["attention"]).eval()
    parameters = sum(p.numel() for p in model.parameters())
    require(parameters < 1_000_000 and all(p.device.type == "cpu" for p in model.parameters()),
            "toy architecture unexpectedly large or non-CPU")
    ids = torch.tensor([[1, 12, 23, 34, 45, 56]], dtype=torch.long)
    labels = ids.clone()
    labels[:, :3] = -100
    with torch.inference_mode():
        output = model(input_ids=ids, attention_mask=torch.ones_like(ids), labels=labels, use_cache=False)
        repeat = model(input_ids=ids, attention_mask=torch.ones_like(ids), use_cache=False)
        manual = -torch.log_softmax(output.logits[0, 2:-1, :].float(), -1).gather(
            -1, ids[0, 3:].unsqueeze(-1)).mean().item()
        native = output.loss.item()
        error = (repeat.logits - output.logits).abs().max().item()
        padded = torch.tensor([[0, 1, 12, 23], [1, 24, 35, 46]], dtype=torch.long)
        generated = model.generate(input_ids=padded, attention_mask=(padded != 0).long(),
            generation_config=GenerationConfig(do_sample=False, num_beams=1, max_new_tokens=3,
                pad_token_id=0, bos_token_id=1, eos_token_id=2, use_cache=True))
    require(torch.isfinite(output.logits).all().item() and abs(native - manual) <= 1e-5 and error <= 1e-5,
            "tiny CPU architecture loss/repeat mismatch")
    require(generated.shape[0] == 2 and 4 < generated.shape[1] <= 7 and
            torch.equal(generated[:, :4], padded), "tiny cached generation failure")
    return {"status":"PASS", "scientific_evidence":False, "weights":"random tiny configuration; not pretrained",
            "parameters":parameters, "native_loss":native, "manual_loss":manual,
            "masked_loss_error":abs(native-manual), "repeat_max_abs":error,
            "generated_shape":list(generated.shape), "torch":torch.__version__,
            "transformers":transformers.__version__, "attention":runtime["attention"]}
