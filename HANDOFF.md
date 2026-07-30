# Handoff: FWA wallet accounting

Everything needed to run a full financial reconciliation of one wallet's
participation in **fwa.fun**. Written for someone with no prior context on this
task. Read to the end before running anything — the "Gotchas" section covers
four ways this produces confidently wrong numbers.

---

## 1. The ask

Produce a complete accounting for wallet:

```
0x5984bb82F11171cb1DC2287E2A6935c44D491538
```

Three questions to answer:

1. **ETH spent** — total, all-in, including gas
2. **ETH received back** — including money returned by contracts
3. **Current value of assets still in the system** — claimable and holdable

The tooling is written and tested. Your job is to run it somewhere with normal
internet access, sanity-check the vault address it guesses, and report the
numbers back.

## 2. Why it couldn't just be run already

The environment the tooling was written in has an egress policy that only
permits GitHub and PyPI. Every block explorer, RPC endpoint, and price API is
refused at the proxy with a `403` to `CONNECT` — verified across curl, Python,
WebFetch, and headless Chromium. It is not a tooling problem and not an API-key
problem; the connection is refused before any request is sent. Hence this
handoff.

## 3. Background: what fwa.fun is

**Fake World Assets (FWA)** by TokenWorks — an on-chain NFT gacha protocol on
Ethereum mainnet. The name parodies the "Real World Assets" narrative.

Three roles:

- **Depositors** deposit an NFT *plus committed ETH backing* — economically
  similar to a Uniswap V2 LP position. They earn a share of every acquisition
  fee, plus FWA token emissions.
- **Purchasers** pay a pool-derived price to receive one randomly selected NFT
  (Chainlink VRF supplies randomness). They can keep it, or sell it back for
  most of its backing ETH.
- **The backing** on each position sets its selection weight *and* funds an
  irrevocable standing bid from the depositor to reacquire the NFT.

Other mechanics that show up in the numbers:

- FWA emissions run at ~1% of total supply daily, split between purchasers and
  depositors.
- One active deposit holds "the crown" and accrues a tithe into a growing pot,
  which pays out to its depositor when that deposit leaves the pool or is
  overtaken.
- Token: `0xa0Df17B5aC76ABaBA36E1450E2cbCd18A620C845` (Ethereum), ~1B supply.
- History note: the v1 contract was exploited via front-running the Chainlink
  VRF callback. It was paused, patched, and the affected owner refunded.

The wallet holder's own description of their activity: spinning and acquiring,
buying FWA, selling FWA, and listing NFTs (both newly acquired and
pre-existing). Expect DEX trades and marketplace sales mixed in with gacha
spins — the tooling separates them.

## 4. Credentials

```
ETHERSCAN_API_KEY=<ASK COIN FOR THIS — sent separately>
```

A free Etherscan key works. Get your own at https://etherscan.io/apis if
preferred. It needs no special permissions — read-only endpoints are all that's
used.

## 5. Run it

Requires Python 3.9+ and bash (macOS, Linux, or WSL).

```bash
git clone https://github.com/coin-creator/playground -b claude/fwa-wallet-accounting-u8qvzg
cd playground
./run_local.sh
```

The script creates a virtualenv, installs dependencies, prompts for the API key
(hidden input, optionally saved to a git-ignored `.env`), validates the key
before doing real work, pulls the full ledger, then reads vault contract state.

Output lands in `reports/<timestamp>/`:

| File | Contents |
|---|---|
| `report.txt` | full console accounting — **the main deliverable** |
| `fwa_audit.xlsx` | Summary / Counterparties / NFT Events / Held NFTs |
| `summary.json` | machine-readable totals |
| `vault_state.txt` | contract reads for backing + unclaimed rewards |

Variants:

```bash
./run_local.sh --vault 0xVaultAddress    # skip vault auto-detection
./run_local.sh --csv ./exports           # offline, from Etherscan CSV exports
./run_local.sh 0xOtherWallet             # a different wallet
```

## 6. How the accounting works

| Line | Derived from |
|---|---|
| ETH spent | outbound value on successful txs **+ gas on every signed tx** |
| ETH received | inbound value **+ internal transfers** |
| Net realised P/L | received − spent |
| FWA position | ERC-20 flows + live balance |
| NFT inventory | ERC-721/1155 logs netted per `(contract, tokenId)` |
| Counterparty split | clustered by address — gacha vs DEX vs marketplace |

Two files do the work:

- **`fwa_audit.py`** — the ledger. Everything that moved on-chain.
- **`fwa_probe.py`** — contract state. Pulls the vault's verified ABI from
  Etherscan (following EIP-1967/1822 proxies to the implementation), finds
  `view` functions taking `()` or `(address)` that return numbers, ranks them by
  name relevance, and `eth_call`s each.

## 7. Gotchas — read this before trusting any number

**a. Internal transactions are where the money comes back.** On FWA, nearly
everything paid *to* the wallet — gacha sell-backs, depositor fee shares, reward
claims, the crown pot — arrives as an *internal* transfer from a contract, not a
normal transaction. An accounting built only on the normal-transaction list
shows heavy spending and almost no return, i.e. a wildly overstated loss. The
tooling pulls internal transfers and merges them. If you ever cross-check by
hand, do not skip them.

**b. Reverted transactions burn gas but move nothing.** A failed spin costs gas
while transferring no ETH. Gas on reverted txs is counted; their value is
excluded. Failed *internal* transfers are excluded entirely — counting them
would invent income that never landed.

**c. The vault address is a guess.** `--vault auto` infers it by ranking
counterparties on volume after excluding known Uniswap / Seaport / Blur / WETH
addresses. The FWA docs were unreachable from the authoring environment, so the
real vault address was never confirmed. **Open the address the script prints on
Etherscan and confirm it is the FWA vault before trusting the gacha-vs-DEX
split.** If it looks wrong, pass the correct one via `--vault`.

**d. Contract reads are heuristic, not authoritative.** A function name does not
establish its meaning — `claimable(address)` means withdrawable ETH on one
protocol and unvested emissions on another. `vault_state.txt` prints the full
signature beside every value. Confirm anything you plan to rely on via
Etherscan's `#readContract` tab. If the vault source is unverified, the script
reads nothing rather than guessing; fall back to the fwa.fun UI (section 8).

**e. If you ever filter tokens, filter by contract address, never by symbol.**
Scam tokens deploy with the real ASCII symbol on a different contract. Symbol
matching cannot distinguish them. Not currently an issue since the FWA token
address is hardcoded, but it matters if you extend the scripts.

**f. Don't put a `.zip` and its own extracted `.csv` files in the same folder**
in offline mode. Both get read. Records are deduped by identity key and
duplicates are reported, so it's handled — but check the log line if a total
looks twice as large as expected.

## 8. The two figures no explorer can see

Both live as contract state and have never moved on-chain, so they appear in no
transfer feed:

1. **Backing ETH in active deposits.** Depositing an NFT commits ETH as backing.
   That ETH shows up in "ETH spent" but is still the wallet's — recoverable by
   withdrawing the position. Omitting it understates the position.
2. **Unclaimed rewards** — accrued fee share, pending FWA emissions, and the
   crown pot if one of the wallet's deposits holds it.

`fwa_probe.py` attempts both. If it can't (unverified contract, or the
auto-detected vault is wrong), read them from https://www.fwa.fun with the
wallet connected.

Final figure:

```
true position = net realised P/L        (report.txt)
              + backing ETH in active deposits
              + unclaimed rewards
              + market value of NFTs held
              + FWA held
```

Note that "net realised P/L" will look worse than reality until the last four
lines are added — most of the value in this system sits in open positions.

## 9. What to report back

1. `report.txt` in full.
2. The vault address the script picked, and whether Etherscan confirms it is
   the FWA vault.
3. `vault_state.txt`, flagging which reads you were able to verify the meaning
   of and which you couldn't.
4. Backing ETH and unclaimed rewards — from the probe if trustworthy, otherwise
   from the fwa.fun UI.
5. Rough market value of the NFTs still held (the `Held NFTs` tab lists them;
   floor prices from OpenSea are fine for a first pass).
6. Anything in the Counterparties tab that looks unexplained — a high-volume
   address that is neither the vault nor a known DEX/marketplace is worth
   investigating.

## 10. Repo contents

| File | Purpose |
|---|---|
| `run_local.sh` | one-command runner — start here |
| `fwa_audit.py` | ledger: ETH/FWA/NFT flows, counterparty clustering |
| `fwa_probe.py` | vault contract state reads |
| `requirements.txt` | Python dependencies |
| `FWA_AUDIT_README.md` | fuller technical detail on both scripts |
| `HANDOFF.md` | this file |

Branch: `claude/fwa-wallet-accounting-u8qvzg` in `coin-creator/playground`.

The scripts' logic was verified offline against fixtures — selector generation
against known ERC-20 values, ABI filtering and decoding, reverted-tx handling,
NFT netting, and agreement between the API and CSV input paths. What was never
exercised is the live network path, since the authoring environment had no
egress. If something fails on a real pull, that is the most likely place for it.

## 11. Security note

The API key is read-only and rate-limited; worst case for a leak is someone
consuming the quota. Still, don't commit it — `.gitignore` already covers
`.env`, `*.key`, and wallet exports. Report contents describe real wallet
activity, so `reports/` is git-ignored too. Rotate the key at
https://etherscan.io/myapikey when this work is done.
