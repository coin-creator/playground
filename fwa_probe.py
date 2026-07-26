#!/usr/bin/env python3
"""
fwa_probe.py — read the two figures no explorer API can see:
  * backing ETH locked in your active FWA deposits
  * unclaimed rewards (fee share, pending FWA emissions, crown pot)

Both live as contract state and have never moved on-chain, so they appear in
no transfer feed. The only way to see them is to call the vault's view
functions directly.

Approach — ABI-driven, not signature-guessing:
  1. resolve the vault address (given, or auto-detected from your ledger)
  2. pull its verified ABI from Etherscan; follow EIP-1967 proxies to the impl
  3. enumerate view/pure functions taking () or (address) and returning numbers
  4. rank them by how likely the name is to mean "claimable" or "backing"
  5. eth_call each one and decode the result

Guessing selectors like `claimable(address)` and hoping is how you end up
reporting a number that is actually something else entirely. Reading the ABI
means every call is one the contract genuinely exposes.

EVERYTHING THIS PRINTS IS A HEURISTIC READ. The script cannot know a protocol's
semantics from a function name. Each result is shown with its full signature so
you can verify the meaning on Etherscan before trusting a number.

Usage:
    export ETHERSCAN_API_KEY=...
    python3 fwa_probe.py 0xYourWallet --vault 0xVaultAddress
    python3 fwa_probe.py 0xYourWallet --vault auto --from-csv ./exports
"""

import argparse
import json
import os
import sys
import time
from decimal import Decimal, getcontext

import requests

getcontext().prec = 40
WEI = Decimal(10) ** 18
ETHERSCAN = "https://api.etherscan.io/v2/api"

# EIP-1967 implementation slot: keccak256("eip1967.proxy.implementation") - 1
EIP1967_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
# EIP-1822 (UUPS) logic slot: keccak256("PROXIABLE")
EIP1822_IMPL = "0xc5f16f0fcc639fa48a6947836d9850f504798523bf8c9a3a87d5876cf622bcf7"

# Name fragments that suggest a function is worth calling, highest signal first.
INTERESTING = [
    (100, ("claimable", "unclaimed", "pendingreward", "pendingrewards")),
    (90,  ("pending", "earned", "owed", "accrued", "harvestable")),
    (80,  ("backing", "backed", "collateral")),
    (70,  ("crown", "pot", "tithe", "topreward", "throne")),
    (60,  ("deposit", "deposits", "position", "positions", "stake", "staked")),
    (40,  ("balance", "shares", "share")),
    (20,  ("total", "reserve", "pool")),
]

NUMERIC = ("uint256", "uint128", "uint96", "uint64", "int256")


def score_name(name):
    low = name.lower().replace("_", "")
    best = 0
    for weight, frags in INTERESTING:
        for f in frags:
            if f in low:
                best = max(best, weight)
    return best


class Chain:
    """Etherscan V2 as an RPC + ABI source. One host, read-only endpoints."""

    def __init__(self, api_key, verbose=True):
        self.key = api_key
        self.verbose = verbose
        self.s = requests.Session()

    def log(self, m):
        if self.verbose:
            print(f"  {m}", file=sys.stderr)

    def call(self, params, attempt=0):
        p = {**params, "chainid": 1, "apikey": self.key}
        try:
            r = self.s.get(ETHERSCAN, params=p, timeout=45)
            if r.status_code in (429, 502, 503) and attempt < 5:
                time.sleep(2 ** attempt)
                return self.call(params, attempt + 1)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt < 5:
                time.sleep(2 ** attempt)
                return self.call(params, attempt + 1)
            raise SystemExit(f"Etherscan unreachable: {e}")

    def get_abi(self, address):
        d = self.call({"module": "contract", "action": "getabi", "address": address})
        if str(d.get("status")) != "1":
            return None
        try:
            return json.loads(d["result"])
        except Exception:
            return None

    def get_source_name(self, address):
        d = self.call({"module": "contract", "action": "getsourcecode", "address": address})
        try:
            return d["result"][0].get("ContractName") or ""
        except Exception:
            return ""

    def storage_at(self, address, slot):
        d = self.call({"module": "proxy", "action": "eth_getStorageAt",
                       "address": address, "position": slot, "tag": "latest"})
        return d.get("result")

    def eth_call(self, to, data):
        d = self.call({"module": "proxy", "action": "eth_call",
                       "to": to, "data": data, "tag": "latest"})
        if "error" in d:
            return None
        return d.get("result")


def resolve_implementation(chain, address):
    """Follow EIP-1967 / EIP-1822 proxies so we read the real ABI."""
    for slot, label in ((EIP1967_IMPL, "EIP-1967"), (EIP1822_IMPL, "EIP-1822")):
        raw = chain.storage_at(address, slot)
        if raw and len(raw) >= 42:
            impl = "0x" + raw[-40:]
            if int(impl, 16) != 0:
                chain.log(f"{label} proxy -> implementation {impl}")
                return impl
    return None


def selector(sig):
    from eth_utils import keccak
    return "0x" + keccak(text=sig)[:4].hex()


def signature(fn):
    return f"{fn['name']}({','.join(i['type'] for i in fn.get('inputs', []))})"


def callable_views(abi):
    """view/pure functions taking nothing or a single address, returning numbers."""
    out = []
    for fn in abi:
        if fn.get("type") != "function":
            continue
        if fn.get("stateMutability") not in ("view", "pure"):
            continue
        ins = fn.get("inputs", [])
        if len(ins) > 1:
            continue
        if len(ins) == 1 and ins[0].get("type") != "address":
            continue
        outs = fn.get("outputs", [])
        if not outs:
            continue
        flat = []
        for o in outs:
            if o.get("type") == "tuple":
                flat.extend(c.get("type") for c in o.get("components", []))
            else:
                flat.append(o.get("type"))
        if not any(t in NUMERIC for t in flat):
            continue
        out.append(fn)
    return out


def decode_result(fn, raw):
    from eth_abi import decode
    types = []
    for o in fn.get("outputs", []):
        if o.get("type") == "tuple":
            types.append("(" + ",".join(c["type"] for c in o.get("components", [])) + ")")
        else:
            types.append(o["type"])
    try:
        return decode(types, bytes.fromhex(raw[2:])), types
    except Exception as e:
        return None, types


def fmt(value, typ):
    if typ in NUMERIC and isinstance(value, int):
        as_eth = Decimal(value) / WEI
        if value == 0:
            return "0"
        # Show both readings; the script cannot know the decimals a priori.
        return f"{value}  (= {as_eth:.6f} if 18-dec)"
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return str(value)


def autodetect_vault(wallet, csv_dir=None, api_key=None):
    """Reuse the audit's counterparty clustering to find the vault."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fwa_audit", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fwa_audit.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    fetcher = (m.CsvFetcher(csv_dir, wallet) if csv_dir
               else m.Fetcher("etherscan", api_key))
    r = m.audit(wallet, fetcher, None, None)
    ranked = sorted(r["counterparties"].items(),
                    key=lambda kv: -(kv[1]["out"] + kv[1]["in"]))
    for addr, v in ranked:
        if m.label(addr):          # skip Uniswap, Seaport, WETH, the token itself
            continue
        if v["txs"] >= 2 or v["out"] > 0:
            return addr, v
    return None, None


def main():
    ap = argparse.ArgumentParser(description="Read FWA vault state for a wallet")
    ap.add_argument("wallet")
    ap.add_argument("--vault", required=True,
                    help="vault contract address, or 'auto' to detect from your ledger")
    ap.add_argument("--from-csv", help="CSV folder, used only when --vault auto")
    ap.add_argument("--api-key", default=os.environ.get("ETHERSCAN_API_KEY"))
    ap.add_argument("--all", action="store_true",
                    help="call every candidate view, not just promising names")
    ap.add_argument("--json", help="write results to JSON")
    args = ap.parse_args()

    if not args.api_key:
        raise SystemExit("ETHERSCAN_API_KEY required (free key from etherscan.io/apis)")

    chain = Chain(args.api_key)

    vault = args.vault
    if vault == "auto":
        print("Auto-detecting vault from counterparty ledger ...", file=sys.stderr)
        vault, stats = autodetect_vault(args.wallet, args.from_csv, args.api_key)
        if not vault:
            raise SystemExit("Could not identify a vault; pass --vault explicitly.")
        print(f"  candidate: {vault} ({stats['txs']} txs, "
              f"{stats['out']:.4f} ETH out, {stats['in']:.4f} ETH in)", file=sys.stderr)
        print("  VERIFY this on Etherscan before trusting the output.\n", file=sys.stderr)

    name = chain.get_source_name(vault)
    print(f"\nVault: {vault}" + (f"  [{name}]" if name else ""))

    abi_addr = vault
    abi = chain.get_abi(vault)
    impl = resolve_implementation(chain, vault)
    if impl:
        impl_abi = chain.get_abi(impl)
        if impl_abi:
            abi, abi_addr = impl_abi, impl
            print(f"Proxy -> implementation {impl} [{chain.get_source_name(impl)}]")

    if not abi:
        print(f"\nNo verified ABI for {abi_addr}. Nothing can be read safely —\n"
              f"the contract source is unverified on Etherscan, so function\n"
              f"signatures are unknown. Read the figures off the fwa.fun UI instead.")
        return

    cands = callable_views(abi)
    scored = sorted(((score_name(f["name"]), f) for f in cands), key=lambda x: -x[0])
    if not args.all:
        scored = [(s, f) for s, f in scored if s > 0]

    print(f"ABI has {len(cands)} callable view functions; "
          f"probing {len(scored)}.\n")
    print("=" * 74)
    print("  HEURISTIC READS — verify each signature's meaning before trusting it")
    print("=" * 74)

    from eth_abi import encode
    wallet_arg = args.wallet
    results = []
    for sc, fn in scored:
        sig = signature(fn)
        data = selector(sig)
        if fn.get("inputs"):
            data += encode(["address"], [wallet_arg]).hex()
        raw = chain.eth_call(vault, data)
        time.sleep(0.22)
        if not raw or raw == "0x":
            continue
        vals, types = decode_result(fn, raw)
        if vals is None:
            continue
        rendered = ", ".join(fmt(v, t) for v, t in zip(vals, types))
        if all(str(v) == "0" for v in vals) and sc < 90:
            continue                     # suppress zero noise from weak matches
        arg = "you" if fn.get("inputs") else ""
        print(f"\n  {sig}{'  <- ' + arg if arg else ''}")
        print(f"      {rendered}")
        results.append({"signature": sig, "score": sc,
                        "values": [str(v) for v in vals], "types": types})

    print("\n" + "=" * 74)
    print("""
  How to read this:

  A name is not a guarantee. `claimable(address)` on one protocol means
  withdrawable ETH; on another it means unvested token emissions. Open
  https://etherscan.io/address/%s#readContract and confirm what each
  function you plan to rely on actually returns.

  Anything showing "= x if 18-dec" is the raw integer reinterpreted as an
  18-decimal fixed-point number. If the value is a token with different
  decimals, or a count, or a timestamp, that second reading is wrong.

  Once you have confirmed the real backing and reward figures:

      true position = NET REALISED (from fwa_audit.py)
                    + backing ETH in active deposits
                    + unclaimed rewards
                    + market value of NFTs held
                    + FWA held
""" % vault)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"vault": vault, "abi_source": abi_addr,
                       "wallet": args.wallet, "reads": results}, fh, indent=2)
        print(f"  JSON written: {args.json}\n")


if __name__ == "__main__":
    main()
