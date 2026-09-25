"""Compatibility entry point for the versioned, resumable canary driver."""
import sys
from pals_validation.campaign import main

if __name__ == "__main__":
    sys.argv.append("--canary-only")
    main()
