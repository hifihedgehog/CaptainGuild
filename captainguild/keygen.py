"""Prints a new master key for .env."""
from .crypto import new_master_key

if __name__ == "__main__":
    print(new_master_key())
