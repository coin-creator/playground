# FWA wallet accounting

Full ETH accounting for participation in [fwa.fun](https://www.fwa.fun) (Fake
World Assets — on-chain NFT gacha protocol, Ethereum mainnet).

Target wallet: `0x5984bb82F11171cb1DC2287E2A6935c44D491538`

## Why this is a script and not a finished report

The session this was written in has an egress policy that blocks every
blockchain data source — Etherscan, Blockscout, Routescan, all public RPC
endpoints, CoinGecko, OpenSea, Reservoir, and `fwa.fun` itself all return
`403` at the proxy. No wallet data could be read, so no real numbers were
produced.

That environment's policy turned out to be a **GitHub + PyPI allowlist** — even
`example.com` is refused — so no chain data source is reachable from it at all.
Run it locally instead.

## Quick start (recommended)

```bash
git clone <this repo> && cd playground
./run_local.sh
```

That creates a virtualenv, installs dependencies, prompts for your Etherscan
key (hidden, optionally saved to a git-ignored `.env`), validates the key
before doing any real work, pulls the full ledger, then reads vault contract
state. Output lands in `reports/<timestamp>/`:

| File | Contents |
|---|---|
| `report.txt` | full console accounting |
| `fwa_audit.xlsx` | Summary / Counterparties / NFT Events / Held NFTs |
| `summary.json` | machine-readable totals |
| `vault_state.txt` | heuristic contract reads |

Other forms:

```bash
./run_local.sh 0xOtherWallet            # different wallet
./run_local.sh --csv ./exports          # offline, from CSV exports
./run_local.sh --vault 0xVault          # skip vault auto-detection
```

Needs bash — macOS, Linux, or WSL. On Windows without WSL, run the two Python
scripts directly as shown below.

The rest of this document covers the pieces individually.

## Offline mode (works anywhere, including from a blocked session)

The audit doesn't need the *tool* to have internet — it needs the *data*.
Etherscan exports it straight from the address page. On
`https://etherscan.io/address/<wallet>`, use the CSV export / "Download Page
Data" button on each tab:

- Transactions
- **Internal Transactions** ← do not skip this one
- ERC-20 Token Txns
- ERC-721 Token Txns
- ERC-1155 Token Txns (if any)

Drop every CSV into one folder — filenames don't matter, each file is
classified by its header row — then:

```bash
python3 fwa_audit.py 0xYourWallet --from-csv ./exports \
    --eth-balance 2.5 --eth-price 3200 --fwa-price 0.0285
```

`--eth-balance` and the prices are optional; offline mode can't look them up,
so pass them if you want the balance and USD columns filled in.

CSV rows are normalised into the same record shape the API returns, so the
identical `audit()` runs over them — both paths were verified to produce
byte-identical totals on the same fixture data.

## Live mode (needs network access)

```bash
pip install requests openpyxl

# free key from https://etherscan.io/apis — strongly preferred
export ETHERSCAN_API_KEY=...
python3 fwa_audit.py 0x5984bb82F11171cb1DC2287E2A6935c44D491538

# keyless fallback (slower, rate-limited)
python3 fwa_audit.py 0x5984bb82F11171cb1DC2287E2A6935c44D491538 --source blockscout
```

Outputs a console summary plus `fwa_audit_<wallet>.xlsx` with tabs for
Summary, Counterparties, NFT Events, and Held NFTs.

## What it computes

| Line | Source |
|---|---|
| ETH spent | outbound value on successful txs + gas on **every** signed tx |
| ETH received | inbound value + **internal** transfers |
| Net realised P/L | received − spent |
| FWA position | ERC-20 flows + live balance |
| NFT inventory | ERC-721/1155 logs netted per `(contract, tokenId)` |
| Counterparty split | clustered by address, separating gacha vs DEX vs marketplace |

### Two things that are easy to get wrong

**Internal transactions.** On FWA almost everything paid back to you — gacha
sell-backs, depositor fee share, reward claims, the crown pot — arrives as an
*internal* transfer from a contract, not a normal transaction. An accounting
built only on `txlist` shows large spending and near-zero return. The script
pulls `txlistinternal` and merges it.

**Reverted transactions.** A failed spin moves no ETH but still burns gas. The
script counts gas on reverted txs and excludes their value. Failed internal
transfers (`isError=1`) are excluded entirely — counting them would invent
income that never landed.

## Reading contract state — `fwa_probe.py`

`fwa_audit.py` sees transfers. It cannot see backing ETH or unclaimed rewards,
because those are contract state that has never moved. `fwa_probe.py` reads
them directly:

```bash
python3 fwa_probe.py 0xYourWallet --vault 0xVaultAddress
python3 fwa_probe.py 0xYourWallet --vault auto --from-csv ./exports
```

It pulls the vault's **verified ABI from Etherscan** (following EIP-1967 /
EIP-1822 proxies to the implementation), enumerates `view`/`pure` functions
taking `()` or `(address)` and returning numbers, ranks them by name relevance,
and `eth_call`s each one.

It reads the ABI rather than guessing selectors like `claimable(address)` on
spec. A guessed signature that happens to collide with a real function returns
a number that looks plausible and means something else entirely.

**Every read is a heuristic.** A name is not a guarantee — `claimable(address)`
means withdrawable ETH on one protocol and unvested emissions on another. The
script prints the full signature next to each value so you can confirm the
meaning on Etherscan's `#readContract` tab before relying on it. If the vault
source is unverified, the script says so and reads nothing rather than
inventing an interpretation.

## What `fwa_audit.py` deliberately cannot value

Two components live as contract state, not as transfers, so no explorer API
can see them:

1. **Backing ETH in active deposits.** Depositing an NFT commits ETH as
   backing. That ETH shows in `TOTAL OUT` but is still yours — recoverable by
   withdrawing the position. Omitting it understates your position.
2. **Unclaimed rewards** — accrued fee share, pending FWA emissions, and the
   crown pot if one of your deposits holds it.

Read both from the fwa.fun UI while connected as this wallet, then:

```
true position = net realised
              + backing ETH in active deposits
              + unclaimed rewards
              + market value of NFTs held
              + FWA held
```

## Contract addresses

Only the FWA ERC-20 is hardcoded: `0xa0Df17B5aC76ABaBA36E1450E2cbCd18A620C845`.

The vault/gacha/reward contracts are **discovered** by clustering counterparties
rather than hardcoded, because they could not be verified from this environment.
The high-volume unlabelled contract in the Counterparties tab is the vault —
confirm it on Etherscan before trusting the gacha-vs-DEX split.
