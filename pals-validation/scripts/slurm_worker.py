"""No SSH/submission side effects; invoked by srun after explicit allocation."""
import os
from pals_validation.run import worker

if __name__ == "__main__":
    os.umask(0o077)
    worker(os.environ["PALS_RUN"], int(os.environ["SLURM_PROCID"]))
