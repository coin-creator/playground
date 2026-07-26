#!/usr/bin/env python3
"""
fwa_audit.py — full ETH accounting for a wallet's participation in fwa.fun
(Fake World Assets, Ethereum mainnet).

Produces a complete reconciliation of:
  * ETH spent   (outbound value + gas on every tx you signed, including reverts)
  * ETH received (inbound value + INTERNAL transfers — sell-backs, claims, payouts)
  * Net position, broken out by counterparty (protocol vs DEX vs marketplace)
  * FWA token flows and current balance
  * NFT inventory acquired / deposited / sold, reconstructed from ERC-721+1155 logs

Why internal transactions matter here: on FWA nearly all ETH coming *back* to you
-- gacha sell-backs, depositor fee shares, the crown pot, reward claims -- arrives
as an internal transfer from a contract, NOT a normal transaction. Any accounting
built only on `txlist` will show you spending ETH and receiving almost nothing
back. That is the single most common way this audit gets it wrong.

Usage:
    export ETHERSCAN_API_KEY=...        # free key from etherscan.io/apis
    python3 fwa_audit.py 0xYourWallet

    # no key? falls back to Blockscout (keyless, slower, rate-limited)
    python3 fwa_audit.py 0xYourWallet --source blockscout

    # pin prices instead of fetching them
    python3 fwa_audit.py 0xYourWallet --eth-price 3200 --fwa-price 0.0285

Output: console summary + fwa_audit_<wallet>.xlsx
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from decimal import Decimal, getcontext

import requests

getcontext().prec = 40
WEI = Decimal(10) ** 18

# --------------------------------------------------------------------------
# Known addresses. Ethereum mainnet.
#
# NOTE: only the FWA ERC-20 is confirmed. The protocol's vault/gacha/reward
# contracts are NOT hardcoded on purpose -- this script DISCOVERS them by
# clustering your counterparties, then lets you label them. Hardcoding a
# guessed vault address is how you silently drop half the ledger.
# --------------------------------------------------------------------------
FWA_TOKEN = "0xa0df17b5ac76ababa36e1450e2cbcd18a620c845".lower()

KNOWN = {
    # DEX / routing -- these are FWA *token* buys and sells, not gacha spins
    "0x66a9893cc07d91d95644aedd05d03f95e1dba8af": "Uniswap V4 UniversalRouter",
    "0x000000000022d473030f116ddee9f6b43ac78ba3": "Permit2",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap V3 SwapRouter02",
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2 Router",
    # NFT marketplaces -- your listings / secondary sales
    "0x0000000000000068f116a894984e2db1123eb395": "Seaport 1.6 (OpenSea)",
    "0x00000000000000adc04c56bf30ac9d3c0aaf14dc": "Seaport 1.5 (OpenSea)",
    "0xb2ecfe4e4d61f8790bbb9de2d1259b9e2410cea5": "Blur Marketplace",
    "0x39da41747a83aee658334415666f3ef92dd0d541": "Blur Pool",
    # Infra
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
}

ETHERSCAN = "https://api.etherscan.io/v2/api"   # V2, chainid=1
BLOCKSCOUT = "https://eth.blockscout.com/api"   # v1-compatible module=account


# --------------------------------------------------------------------------
# Fetch layer
# --------------------------------------------------------------------------
class Fetcher:
    def __init__(self, source, api_key=None, verbose=True):
        self.source = source
        self.api_key = api_key
        self.verbose = verbose
        self.session = requests.Session()

    def log(self, msg):
        if self.verbose:
            print(f"  {msg}", file=sys.stderr)

    def _get(self, params, attempt=0):
        base = ETHERSCAN if self.source == "etherscan" else BLOCKSCOUT
        if self.source == "etherscan":
            params = {**params, "chainid": 1, "apikey": self.api_key}
        try:
            r = self.session.get(base, params=params, timeout=45)
            if r.status_code in (429, 502, 503) and attempt < 5:
                wait = 2 ** attempt
                self.log(f"HTTP {r.status_code}, backing off {wait}s")
                time.sleep(wait)
                return self._get(params, attempt + 1)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt < 5:
                wait = 2 ** attempt
                self.log(f"{type(e).__name__}, retry in {wait}s")
                time.sleep(wait)
                return self._get(params, attempt + 1)
            raise

    def paginate(self, action, address, extra=None):
        """Pull an account endpoint to completion. Never trust page 1."""
        out, page, offset = [], 1, 1000
        while True:
            params = {
                "module": "account", "action": action, "address": address,
                "startblock": 0, "endblock": 99999999,
                "page": page, "offset": offset, "sort": "asc",
            }
            if extra:
                params.update(extra)
            data = self._get(params)
            result = data.get("result")
            if not isinstance(result, list):
                # Etherscan returns a string message for "No transactions found"
                if data.get("message") and "No " in str(data.get("message")):
                    break
                self.log(f"unexpected result for {action}: {str(result)[:120]}")
                break
            out.extend(result)
            self.log(f"{action}: page {page} -> {len(result)} (total {len(out)})")
            if len(result) < offset:
                break
            page += 1
            time.sleep(0.25)
        return out

    def eth_balance(self, address):
        d = self._get({"module": "account", "action": "balance",
                       "address": address, "tag": "latest"})
        return Decimal(d.get("result", 0)) / WEI

    def token_balance(self, address, token):
        d = self._get({"module": "account", "action": "tokenbalance",
                       "contractaddress": token, "address": address, "tag": "latest"})
        try:
            return Decimal(d.get("result", 0))
        except Exception:
            return Decimal(0)


# --------------------------------------------------------------------------
# Accounting
# --------------------------------------------------------------------------
def audit(wallet, fetcher, eth_price, fwa_price):
    w = wallet.lower()
    print(f"\nPulling on-chain history for {wallet} ...", file=sys.stderr)

    normal   = fetcher.paginate("txlist", wallet)
    internal = fetcher.paginate("txlistinternal", wallet)
    erc20    = fetcher.paginate("tokentx", wallet)
    erc721   = fetcher.paginate("tokennfttx", wallet)
    try:
        erc1155 = fetcher.paginate("token1155tx", wallet)
    except Exception:
        erc1155 = []

    # ---- ETH out: value on successful outbound txs, gas on ALL signed txs ----
    eth_out_value = Decimal(0)
    gas_total     = Decimal(0)
    eth_in_normal = Decimal(0)
    reverted_count = 0

    # per-counterparty ledger
    cp = defaultdict(lambda: {"out": Decimal(0), "in": Decimal(0),
                              "gas": Decimal(0), "txs": 0, "reverts": 0})

    for t in normal:
        frm = (t.get("from") or "").lower()
        to  = (t.get("to") or "").lower()
        val = Decimal(t.get("value", 0)) / WEI
        failed = str(t.get("isError", "0")) == "1"

        if frm == w:
            # Gas is spent whether or not the tx succeeded.
            gas = (Decimal(t.get("gasUsed", 0)) *
                   Decimal(t.get("gasPrice", 0))) / WEI
            gas_total += gas
            cp[to]["gas"] += gas
            cp[to]["txs"] += 1
            if failed:
                reverted_count += 1
                cp[to]["reverts"] += 1
            else:
                eth_out_value += val
                cp[to]["out"] += val
        elif to == w and not failed:
            eth_in_normal += val
            cp[frm]["in"] += val
            cp[frm]["txs"] += 1

    # ---- ETH in: internal transfers. This is where FWA pays you back. ----
    eth_in_internal = Decimal(0)
    eth_out_internal = Decimal(0)
    for t in internal:
        if str(t.get("isError", "0")) == "1":
            continue
        frm = (t.get("from") or "").lower()
        to  = (t.get("to") or "").lower()
        val = Decimal(t.get("value", 0)) / WEI
        if to == w:
            eth_in_internal += val
            cp[frm]["in"] += val
        elif frm == w:
            eth_out_internal += val
            cp[to]["out"] += val

    total_out = eth_out_value + eth_out_internal + gas_total
    total_in  = eth_in_normal + eth_in_internal
    net       = total_in - total_out

    # ---- FWA token flows ----
    fwa_in = fwa_out = Decimal(0)
    fwa_decimals = 18
    for t in erc20:
        if (t.get("contractAddress") or "").lower() != FWA_TOKEN:
            continue
        fwa_decimals = int(t.get("tokenDecimal") or 18)
        amt = Decimal(t.get("value", 0)) / (Decimal(10) ** fwa_decimals)
        if (t.get("to") or "").lower() == w:
            fwa_in += amt
        elif (t.get("from") or "").lower() == w:
            fwa_out += amt

    # ---- NFT inventory: net position per (collection, tokenId) ----
    held = {}
    nft_events = []
    for t in list(erc721) + list(erc1155):
        c    = (t.get("contractAddress") or "").lower()
        tid  = t.get("tokenID") or t.get("tokenId") or ""
        name = t.get("tokenName") or "?"
        qty  = Decimal(t.get("tokenValue") or 1)
        key  = (c, str(tid))
        direction = None
        if (t.get("to") or "").lower() == w:
            held[key] = held.get(key, Decimal(0)) + qty
            direction = "IN"
        elif (t.get("from") or "").lower() == w:
            held[key] = held.get(key, Decimal(0)) - qty
            direction = "OUT"
        if direction:
            nft_events.append({
                "hash": t.get("hash"), "ts": int(t.get("timeStamp", 0)),
                "collection": name, "contract": c, "tokenId": str(tid),
                "direction": direction,
                "counterparty": (t.get("from") if direction == "IN" else t.get("to")),
            })
    still_held = {k: v for k, v in held.items() if v > 0}

    # ---- balances now ----
    eth_bal = fetcher.eth_balance(wallet)
    fwa_bal_raw = fetcher.token_balance(wallet, FWA_TOKEN)
    fwa_bal = fwa_bal_raw / (Decimal(10) ** fwa_decimals)

    return {
        "wallet": wallet,
        "eth_out_value": eth_out_value, "eth_out_internal": eth_out_internal,
        "gas_total": gas_total, "total_out": total_out,
        "eth_in_normal": eth_in_normal, "eth_in_internal": eth_in_internal,
        "total_in": total_in, "net": net,
        "reverted_count": reverted_count, "tx_count": len(normal),
        "fwa_in": fwa_in, "fwa_out": fwa_out, "fwa_bal": fwa_bal,
        "eth_bal": eth_bal,
        "counterparties": cp, "still_held": still_held, "nft_events": nft_events,
        "eth_price": eth_price, "fwa_price": fwa_price,
        "counts": {"normal": len(normal), "internal": len(internal),
                   "erc20": len(erc20), "erc721": len(erc721), "erc1155": len(erc1155)},
    }


# --------------------------------------------------------------------------
# Offline mode — Etherscan CSV exports
#
# Needs no network at all. On the Etherscan address page, each tab has a
# "Download Page Data" / CSV Export button:
#     Transactions -> Internal Transactions -> ERC-20 Token Txns
#                  -> ERC-721 Token Txns    -> ERC-1155 Token Txns
# Drop every CSV into one folder and point --from-csv at it. Filenames do not
# matter; each file is classified by its header row.
#
# Rows are normalised into the exact record shape the API returns, so the same
# audit() runs over them and the arithmetic is identical either way.
# --------------------------------------------------------------------------
import csv
import glob


def _n(s):
    """Normalise a header cell so column matching survives Etherscan's
    formatting churn: 'Value_IN(ETH)' -> 'valueineth'."""
    out = []
    for ch in (s or ""):
        if ch.isalnum():
            out.append(ch.lower())
    return "".join(out)


def _dec(s):
    """Etherscan writes '1,234.56', '' and sometimes '0E-18'."""
    s = (s or "").strip().replace(",", "").replace('"', "")
    if not s:
        return Decimal(0)
    try:
        return Decimal(s)
    except Exception:
        return Decimal(0)


class CsvFetcher:
    def __init__(self, directory, wallet, eth_balance=None, verbose=True):
        self.dir = directory
        self.w = wallet.lower()
        self._eth_bal = Decimal(str(eth_balance)) if eth_balance is not None else None
        self.verbose = verbose
        self.buckets = {k: [] for k in
                        ("txlist", "txlistinternal", "tokentx", "tokennfttx", "token1155tx")}
        self._fwa_net = Decimal(0)
        self._failed_seen = 0
        self._load()

    def log(self, msg):
        if self.verbose:
            print(f"  {msg}", file=sys.stderr)

    # -- column resolution ------------------------------------------------
    @staticmethod
    def _col(fields, *candidates, avoid=()):
        norm = {_n(f): f for f in fields}
        for c in candidates:
            if c in norm:
                return norm[c]
        for c in candidates:                      # substring fallback
            for nf, orig in norm.items():
                if c in nf and not any(a in nf for a in avoid):
                    return orig
        return None

    @staticmethod
    def _classify(fields):
        n = {_n(f) for f in fields}
        if any("parenttx" in f for f in n):
            return "txlistinternal"
        has_tokenid = any(f in ("tokenid", "tokenids") for f in n)
        has_tokenval = any("tokenvalue" in f or "tokenqty" in f for f in n)
        if has_tokenid and has_tokenval:
            return "token1155tx"
        if has_tokenid:
            return "tokennfttx"
        if any("tokensymbol" in f or "tokenname" in f for f in n) or has_tokenval:
            return "tokentx"
        if any("txnfee" in f for f in n) or "method" in n:
            return "txlist"
        return None

    @staticmethod
    def _failed(row, fields):
        """Etherscan marks reverts inconsistently across export versions:
        a 'Status' of 'Error(0)', a non-empty 'ErrCode', or Status == '1'."""
        for f in fields:
            nf = _n(f)
            if nf in ("status", "errcode", "error"):
                v = (row.get(f) or "").strip().lower()
                if not v:
                    continue
                if "error" in v or "fail" in v or "revert" in v:
                    return True
                if nf == "status" and v == "1":
                    return True
        return False

    # -- loading ----------------------------------------------------------
    def _load(self):
        files = sorted(glob.glob(os.path.join(self.dir, "*.csv")))
        if not files:
            raise SystemExit(f"No .csv files found in {self.dir}")
        for path in files:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
            if not rows:
                self.log(f"{os.path.basename(path)}: empty, skipped")
                continue
            fields = list(rows[0].keys())
            kind = self._classify(fields)
            if not kind:
                self.log(f"{os.path.basename(path)}: unrecognised header, skipped")
                continue
            before = len(self.buckets[kind])
            getattr(self, f"_load_{kind}")(rows, fields)
            self.log(f"{os.path.basename(path)}: {kind} "
                     f"+{len(self.buckets[kind]) - before} rows")

    def _common(self, row, fields):
        h = self._col(fields, "transactionhash", "txhash", "hash")
        t = self._col(fields, "unixtimestamp", "timestamp")
        return ((row.get(h) or "").strip(),
                str(int(_dec(row.get(t)))) if t else "0")

    def _load_txlist(self, rows, fields):
        c_from = self._col(fields, "from")
        c_to   = self._col(fields, "to", avoid=("contract",))
        c_ctr  = self._col(fields, "contractaddress")
        c_in   = self._col(fields, "valueineth", "valuein")
        c_out  = self._col(fields, "valueouteth", "valueout")
        c_fee  = self._col(fields, "txnfeeeth", "txnfee")
        for r in rows:
            h, ts = self._common(r, fields)
            failed = self._failed(r, fields)
            self._failed_seen += bool(failed)
            frm = (r.get(c_from) or "").strip()
            to  = (r.get(c_to) or "").strip() or (r.get(c_ctr) or "").strip()
            val = _dec(r.get(c_in)) + _dec(r.get(c_out))
            fee = _dec(r.get(c_fee))
            # audit() computes gas as gasUsed*gasPrice/WEI; the CSV gives the
            # fee directly, so express it as (fee_in_wei x 1) for an exact match.
            self.buckets["txlist"].append({
                "from": frm, "to": to, "hash": h, "timeStamp": ts,
                "value": str(int(val * WEI)),
                "isError": "1" if failed else "0",
                "gasUsed": str(int(fee * WEI)) if frm.lower() == self.w else "0",
                "gasPrice": "1",
            })

    def _load_txlistinternal(self, rows, fields):
        c_from = self._col(fields, "from", avoid=("parent",))
        c_to   = self._col(fields, "txto", "to", avoid=("parent", "contract"))
        c_in   = self._col(fields, "valueineth", "valuein")
        c_out  = self._col(fields, "valueouteth", "valueout")
        for r in rows:
            h, ts = self._common(r, fields)
            self.buckets["txlistinternal"].append({
                "from": (r.get(c_from) or "").strip(),
                "to": (r.get(c_to) or "").strip(),
                "hash": h, "timeStamp": ts,
                "value": str(int((_dec(r.get(c_in)) + _dec(r.get(c_out))) * WEI)),
                "isError": "1" if self._failed(r, fields) else "0",
            })

    def _load_tokentx(self, rows, fields):
        c_from = self._col(fields, "from")
        c_to   = self._col(fields, "to", avoid=("contract",))
        c_ctr  = self._col(fields, "contractaddress")
        c_val  = self._col(fields, "tokenvalue", "tokenqty", "value")
        c_sym  = self._col(fields, "tokensymbol")
        for r in rows:
            h, ts = self._common(r, fields)
            amt = _dec(r.get(c_val))
            ctr = (r.get(c_ctr) or "").strip()
            # tokenDecimal "0" => audit() divides by 1, so pass human units.
            self.buckets["tokentx"].append({
                "from": (r.get(c_from) or "").strip(),
                "to": (r.get(c_to) or "").strip(),
                "contractAddress": ctr, "value": str(amt), "tokenDecimal": "0",
                "tokenSymbol": (r.get(c_sym) or "").strip(),
                "hash": h, "timeStamp": ts,
            })
            if ctr.lower() == FWA_TOKEN:
                if (r.get(c_to) or "").strip().lower() == self.w:
                    self._fwa_net += amt
                elif (r.get(c_from) or "").strip().lower() == self.w:
                    self._fwa_net -= amt

    def _load_nft(self, rows, fields, bucket, with_qty):
        c_from = self._col(fields, "from")
        c_to   = self._col(fields, "to", avoid=("contract",))
        c_ctr  = self._col(fields, "contractaddress")
        c_id   = self._col(fields, "tokenid", "tokenids")
        c_name = self._col(fields, "tokenname")
        c_qty  = self._col(fields, "tokenvalue", "tokenqty") if with_qty else None
        for r in rows:
            h, ts = self._common(r, fields)
            rec = {
                "from": (r.get(c_from) or "").strip(),
                "to": (r.get(c_to) or "").strip(),
                "contractAddress": (r.get(c_ctr) or "").strip(),
                "tokenID": (r.get(c_id) or "").strip(),
                "tokenName": (r.get(c_name) or "").strip() or "?",
                "hash": h, "timeStamp": ts,
            }
            if with_qty:
                rec["tokenValue"] = str(_dec(r.get(c_qty)) or 1)
            self.buckets[bucket].append(rec)

    def _load_tokennfttx(self, rows, fields):
        self._load_nft(rows, fields, "tokennfttx", with_qty=False)

    def _load_token1155tx(self, rows, fields):
        self._load_nft(rows, fields, "token1155tx", with_qty=True)

    # -- Fetcher interface ------------------------------------------------
    def paginate(self, action, address, extra=None):
        return self.buckets.get(action, [])

    def eth_balance(self, address):
        if self._eth_bal is None:
            self.log("no --eth-balance given; wallet ETH balance reported as 0")
            return Decimal(0)
        return self._eth_bal

    def token_balance(self, address, token):
        # tokentx rows carry tokenDecimal "0", so audit() divides by 1 —
        # return the net flow in the same human units.
        return self._fwa_net if token.lower() == FWA_TOKEN else Decimal(0)


def label(addr):
    a = (addr or "").lower()
    if a == FWA_TOKEN:
        return "FWA token (ERC-20)"
    return KNOWN.get(a, "")


def report(r):
    e = r["eth_price"]
    m = lambda x: f"  (${x * e:,.0f})" if e else ""
    p = print

    p("\n" + "=" * 72)
    p(f"  FWA WALLET ACCOUNTING — {r['wallet']}")
    p("=" * 72)
    c = r["counts"]
    p(f"\nRecords pulled: {c['normal']} normal, {c['internal']} internal, "
      f"{c['erc20']} ERC-20, {c['erc721']} ERC-721, {c['erc1155']} ERC-1155")

    p("\n--- ETH OUT " + "-" * 59)
    p(f"  Value sent (successful txs) {r['eth_out_value']:>14.6f} ETH{m(r['eth_out_value'])}")
    if r["eth_out_internal"]:
        p(f"  Value sent (internal)       {r['eth_out_internal']:>14.6f} ETH")
    gas_label = f"Gas ({r['tx_count']} txs, {r['reverted_count']} reverted)"
    p(f"  {gas_label:<27} {r['gas_total']:>14.6f} ETH{m(r['gas_total'])}")
    p(f"  {'TOTAL OUT':<27} {r['total_out']:>14.6f} ETH{m(r['total_out'])}")

    p("\n--- ETH IN " + "-" * 60)
    p(f"  Normal inbound              {r['eth_in_normal']:>14.6f} ETH{m(r['eth_in_normal'])}")
    p(f"  Internal (sell-backs,       {r['eth_in_internal']:>14.6f} ETH{m(r['eth_in_internal'])}")
    p(f"    claims, payouts)")
    p(f"  {'TOTAL IN':<27} {r['total_in']:>14.6f} ETH{m(r['total_in'])}")

    p("\n--- NET " + "-" * 63)
    sign = "+" if r["net"] >= 0 else ""
    p(f"  {'REALISED ETH P/L':<27} {sign}{r['net']:>13.6f} ETH{m(r['net'])}")
    p("  (realised only — excludes NFTs held, FWA held, and ETH still")
    p("   locked as deposit backing inside the protocol)")

    p("\n--- STILL IN THE SYSTEM / HELD " + "-" * 40)
    p(f"  Wallet ETH balance          {r['eth_bal']:>14.6f} ETH{m(r['eth_bal'])}")
    fp = r["fwa_price"]
    fv = f"  (${r['fwa_bal'] * fp:,.0f})" if fp else ""
    p(f"  FWA balance                 {r['fwa_bal']:>14.4f} FWA{fv}")
    p(f"    (received {r['fwa_in']:.4f}, sent {r['fwa_out']:.4f})")
    p(f"  NFTs currently held         {len(r['still_held']):>14} positions")

    by_col = defaultdict(int)
    for (contract, _tid) in r["still_held"]:
        nm = next((e["collection"] for e in r["nft_events"]
                   if e["contract"] == contract), contract[:10])
        by_col[nm] += 1
    for nm, n in sorted(by_col.items(), key=lambda kv: -kv[1]):
        p(f"      {n:>4} x {nm}")

    p("\n--- COUNTERPARTIES (ranked by ETH out) " + "-" * 32)
    rows = sorted(r["counterparties"].items(),
                  key=lambda kv: -(kv[1]["out"] + kv[1]["gas"]))
    p(f"  {'address':<44}{'out':>12}{'in':>12}{'net':>12}  txs")
    for addr, v in rows[:25]:
        if v["out"] == 0 and v["in"] == 0 and v["gas"] == 0:
            continue
        n = v["in"] - v["out"] - v["gas"]
        tag = label(addr)
        p(f"  {addr:<44}{v['out']:>12.4f}{v['in']:>12.4f}{n:>12.4f}  {v['txs']}")
        if tag:
            p(f"      ^ {tag}")

    p("\n" + "=" * 72)
    p("  NOT CAPTURED ON-CHAIN BY THIS SCRIPT")
    p("=" * 72)
    p("""
  Two figures need the fwa.fun UI (or the vault ABI) to value, because they
  live as contract state, not as transfers:

    1. Backing ETH in your ACTIVE deposits. When you deposit an NFT you commit
       ETH as backing. That ETH left your wallet (it IS in 'TOTAL OUT' above)
       but it is still yours -- recoverable by withdrawing the position. Count
       it as an asset, or you will understate your position.

    2. Unclaimed rewards: accrued fee share, pending FWA emissions, and the
       crown pot if one of your deposits holds it. Accrued-but-unclaimed value
       has never moved on-chain, so no explorer API can see it.

  Read both off https://www.fwa.fun while connected as this wallet, then:

       true position = NET (above) + backing ETH + unclaimed rewards
                     + market value of NFTs held + FWA held

  Also note: the counterparty table separates gacha spins from FWA token
  buys/sells. Uniswap rows are token trading; the unlabelled high-volume
  contract you interact with repeatedly is the FWA vault -- verify it on
  Etherscan before trusting the split.
""")


def write_xlsx(r, path):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError:
        print("openpyxl not installed; skipping spreadsheet", file=sys.stderr)
        return None

    wb = Workbook()
    hdr = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="365F91")

    def head(ws, cols):
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = hdr, fill

    ws = wb.active
    ws.title = "Summary"
    head(ws, ["Metric", "ETH", "USD"])
    e = r["eth_price"]
    for k, v in [
        ("ETH sent (value)", r["eth_out_value"]),
        ("ETH sent (internal)", r["eth_out_internal"]),
        ("Gas paid", r["gas_total"]),
        ("TOTAL OUT", r["total_out"]),
        ("ETH in (normal)", r["eth_in_normal"]),
        ("ETH in (internal)", r["eth_in_internal"]),
        ("TOTAL IN", r["total_in"]),
        ("NET REALISED", r["net"]),
        ("Wallet ETH balance", r["eth_bal"]),
    ]:
        ws.append([k, float(v), float(v * e) if e else None])
    ws.append([])
    ws.append(["FWA balance", float(r["fwa_bal"]),
               float(r["fwa_bal"] * r["fwa_price"]) if r["fwa_price"] else None])
    ws.append(["NFT positions held", len(r["still_held"]), None])
    ws.append([])
    ws.append(["ADD MANUALLY from fwa.fun UI:"])
    ws.append(["Backing ETH in active deposits", None, None])
    ws.append(["Unclaimed rewards (fees + FWA + crown pot)", None, None])

    ws2 = wb.create_sheet("Counterparties")
    head(ws2, ["Address", "Label", "ETH out", "ETH in", "Gas", "Net", "Txs", "Reverts", "Explorer"])
    for addr, v in sorted(r["counterparties"].items(),
                          key=lambda kv: -(kv[1]["out"] + kv[1]["gas"])):
        ws2.append([addr, label(addr), float(v["out"]), float(v["in"]),
                    float(v["gas"]), float(v["in"] - v["out"] - v["gas"]),
                    v["txs"], v["reverts"], f"https://etherscan.io/address/{addr}"])

    ws3 = wb.create_sheet("NFT Events")
    head(ws3, ["Time", "Collection", "Contract", "TokenID", "Direction", "Counterparty", "Tx"])
    for ev in sorted(r["nft_events"], key=lambda x: x["ts"]):
        ws3.append([
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(ev["ts"])),
            ev["collection"], ev["contract"], ev["tokenId"], ev["direction"],
            ev["counterparty"], f"https://etherscan.io/tx/{ev['hash']}"])

    ws4 = wb.create_sheet("Held NFTs")
    head(ws4, ["Contract", "TokenID", "Qty", "Collection", "Explorer"])
    for (contract, tid), qty in sorted(r["still_held"].items()):
        nm = next((e["collection"] for e in r["nft_events"]
                   if e["contract"] == contract), "")
        ws4.append([contract, tid, float(qty), nm,
                    f"https://etherscan.io/token/{contract}?a={tid}"])

    for sheet in wb:
        for col in sheet.columns:
            width = max((len(str(c.value)) for c in col if c.value), default=10)
            sheet.column_dimensions[col[0].column_letter].width = min(width + 2, 60)

    wb.save(path)
    return path


def fetch_prices(args):
    eth, fwa = args.eth_price, args.fwa_price
    if eth and fwa:
        return Decimal(str(eth)), Decimal(str(fwa))
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum,fake-world-assets", "vs_currencies": "usd"},
            timeout=20).json()
        eth = eth or r.get("ethereum", {}).get("usd")
        fwa = fwa or r.get("fake-world-assets", {}).get("usd")
    except Exception as ex:
        print(f"price fetch failed ({ex}); pass --eth-price/--fwa-price", file=sys.stderr)
    return (Decimal(str(eth)) if eth else None), (Decimal(str(fwa)) if fwa else None)


def main():
    ap = argparse.ArgumentParser(description="FWA wallet ETH accounting")
    ap.add_argument("wallet")
    ap.add_argument("--source", choices=["etherscan", "blockscout"], default="etherscan")
    ap.add_argument("--api-key", default=os.environ.get("ETHERSCAN_API_KEY"))
    ap.add_argument("--from-csv", metavar="DIR",
                    help="offline mode: folder of Etherscan CSV exports (no network)")
    ap.add_argument("--eth-balance", type=float,
                    help="current wallet ETH balance (offline mode can't read it)")
    ap.add_argument("--eth-price", type=float)
    ap.add_argument("--fwa-price", type=float)
    ap.add_argument("--out")
    ap.add_argument("--json", help="also dump raw totals as JSON")
    args = ap.parse_args()

    if args.from_csv:
        f = CsvFetcher(args.from_csv, args.wallet, eth_balance=args.eth_balance)
        eth_price = Decimal(str(args.eth_price)) if args.eth_price else None
        fwa_price = Decimal(str(args.fwa_price)) if args.fwa_price else None
        if not eth_price:
            print("Tip: pass --eth-price/--fwa-price for USD columns.", file=sys.stderr)
    else:
        if args.source == "etherscan" and not args.api_key:
            print("No ETHERSCAN_API_KEY set — falling back to Blockscout.", file=sys.stderr)
            args.source = "blockscout"
        eth_price, fwa_price = fetch_prices(args)
        f = Fetcher(args.source, args.api_key)

    r = audit(args.wallet, f, eth_price, fwa_price)
    report(r)

    out = args.out or f"fwa_audit_{args.wallet[:10]}.xlsx"
    if write_xlsx(r, out):
        print(f"\nSpreadsheet written: {out}\n")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({k: (float(v) if isinstance(v, Decimal) else v)
                       for k, v in r.items()
                       if isinstance(v, (Decimal, int, str))}, fh, indent=2)


if __name__ == "__main__":
    main()
