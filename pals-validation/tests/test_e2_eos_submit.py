"""Mock scheduler contract tests for the one-shot score-only recovery chain."""
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pals_validation.io import read

FILE = Path(__file__).parents[1] / "scripts/e2_eos_submit.py"
SPEC = importlib.util.spec_from_file_location("eos_submit", FILE)
M = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(M)


class SubmitTests(unittest.TestCase):
    @contextmanager
    def scheduler(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/"submissions").mkdir()
            states, calls, releases = {}, [], []
            def output(cmd, **kw):
                if cmd[0]=="squeue": return "\n".join(j+" "+r["JobName"] for j,r in states.items())
                if cmd[0]=="scontrol": return " ".join(k+"="+v for k,v in states[cmd[3]].items())
                self.assertEqual(cmd[0],"sbatch")
                self.assertFalse(any(k.startswith("SBATCH_") for k in kw["env"]))
                calls.append(cmd); job=str(100+len(calls)); gpu=len(calls)==2
                options=dict(x[2:].split("=",1) for x in cmd if x.startswith("--") and "=" in x)
                states[job]={"JobId":job,"JobName":options["job-name"],"JobState":"PENDING","Priority":"0",
                    "Partition":"defq","Reservation":"code-agent","ExcNodeList":M.EXCLUDE,"NumTasks":"1",
                    "NumCPUs":"32" if gpu else "8","CPUs/Task":"32" if gpu else "8","TimeLimit":"01:00:00",
                    "Requeue":"0","Restarts":"0","Command":str(M.LAUNCHER),"WorkDir":str(root),
                    "StdOut":options["output"].replace("%j",job),"StdErr":options["error"].replace("%j",job),
                    "UserId":"xijun(101112)","NumNodes":"1-1",
                    "ReqTRES":"cpu=32,mem=256G,node=1,gres/gpu=8" if gpu else "cpu=8,mem=64G,node=1",
                    "Dependency":options.get("dependency","(null)"),"KillOInInvalidDependent":"Yes"}
                return job
            def run(cmd,**kw):
                self.assertEqual(cmd[:2],["scontrol","release"]); releases.append(cmd[2])
                return subprocess.CompletedProcess(cmd,0)
            with patch.object(M,"ROOT",root),patch.object(M,"check"), \
                 patch.object(M.subprocess,"check_output",side_effect=output),patch.object(M.subprocess,"run",side_effect=run):
                yield root,states,calls,releases

    def test_only_middle_stage_requests_gpu_and_no_generation(self):
        with self.scheduler() as (root,states,calls,releases), patch.dict(os.environ,{"SBATCH_GRES":"gpu:99"}):
            records=M.submit(); self.assertEqual([r["stage"] for r in records],["prepare","launch","audit"])
            self.assertIn("--gres=none",calls[0]); self.assertIn("--gres=none",calls[2])
            self.assertIn("--gpus-per-node=8",calls[1]); self.assertEqual(releases,[])
            self.assertEqual(states["102"]["Dependency"],"afterok:101")
            M.release(); self.assertEqual(releases,["103","102","101"])
            with self.assertRaisesRegex(ValueError,"existing/uncertain"): M.submit()
            with self.assertRaisesRegex(ValueError,"already attempted"): M.release()

    def test_bad_resources_dependency_or_identity_never_release(self):
        for key,value in {"Priority":"1","Reservation":"evaluation","ExcNodeList":"(null)",
                          "Dependency":"(null)","UserId":"other(1)","TimeLimit":"02:00:00",
                          "ReqTRES":"cpu=32,mem=256G,node=1,gres/gpu=16","JobState":"RUNNING"}.items():
            with self.subTest(key=key), self.scheduler() as (_,states,_,releases):
                M.submit(); states["102"][key]=value
                with self.assertRaises(ValueError): M.release()
                self.assertEqual(releases,[])

    def test_cpu_stage_cannot_accidentally_allocate_gpu(self):
        with self.scheduler() as (_,states,_,releases):
            M.submit(); states["101"]["ReqTRES"] += ",gres/gpu=1"
            with self.assertRaisesRegex(ValueError,"resources"): M.release()
            self.assertEqual(releases,[])

    def test_uncertain_submit_stops_without_retry(self):
        with self.scheduler() as (root,_,_,_):
            previous=M.subprocess.check_output.side_effect
            def ambiguous(cmd,**kw): return "lost" if cmd[0]=="sbatch" else previous(cmd,**kw)
            with patch.object(M.subprocess,"check_output",side_effect=ambiguous):
                with self.assertRaisesRegex(ValueError,"uncertain sbatch"): M.submit()
            self.assertTrue((root/"submissions/0-attempt.json").exists())
            with self.assertRaisesRegex(ValueError,"existing/uncertain"): M.submit()

    def test_partial_release_never_retries(self):
        with self.scheduler() as (_,_,_,releases):
            M.submit()
            with patch.object(M.subprocess,"run",side_effect=subprocess.CalledProcessError(1,[])):
                with self.assertRaises(subprocess.CalledProcessError): M.release()
            with self.assertRaisesRegex(ValueError,"already attempted"): M.release()

    def test_other_active_pals_blocks_submission(self):
        with self.scheduler() as (_,states,calls,_):
            states["777"]={"JobName":"pals-llama-gpqa-e2"}
            with self.assertRaisesRegex(ValueError,"another B1"): M.submit()
            self.assertEqual(calls,[])

    def test_scientific_all_three_states_required(self):
        for suffix in ("", ".batch", ".0"):
            rows={"1"+s:["COMPLETED","0:0"] for s in ("", ".batch", ".0")}
            rows["1"+suffix]=["FAILED","1:0"]
            with patch.object(M,"scheduler_rows",return_value=rows):
                with self.assertRaisesRegex(ValueError,"scientifically complete"): M.successful("1")


if __name__=="__main__": unittest.main()
