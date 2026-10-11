"""Generate a home-network entry for config.yaml without writing the passphrase.

    python3 -m minidriver.psk my-home-wifi

Prompts for the passphrase rather than taking it as an argument: an argument
would sit in shell history and be visible in `ps` to everyone on the box. It
prints a `psk_hash` entry to paste under network.home_networks -- the derived key
works for associating, and the passphrase never lands in a file. (The derived key
still grants access to the network, so config.yaml stays a 0600 secret regardless.)
"""
import getpass
import sys

from minidriver.netconfig import psk_hash


def format_entry(ssid: str, passphrase: str) -> str:
    """The paste-ready YAML list item for one home network."""
    return (f"  - ssid: {ssid}\n"
            f'    psk_hash: "{psk_hash(ssid, passphrase)}"\n'
            f"    priority: 10")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 -m minidriver.psk <ssid>", file=sys.stderr)
        return 2
    ssid = argv[0]

    passphrase = getpass.getpass("Passphrase: ")
    if not passphrase:
        print("empty passphrase refused", file=sys.stderr)
        return 1
    if passphrase != getpass.getpass("Confirm: "):
        print("passphrases did not match", file=sys.stderr)
        return 1

    print()
    print("Add this under network.home_networks in /etc/minidriver/config.yaml:")
    print()
    print(format_entry(ssid, passphrase))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
