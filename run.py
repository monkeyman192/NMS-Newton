import os.path as op

from pymhf import load_module

if __name__ == "__main__":
    load_module("Newton", op.dirname(__file__))
