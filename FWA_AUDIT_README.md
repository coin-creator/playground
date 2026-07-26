# FWA wallet accounting

Full ETH accounting for participation in [fwa.fun](https://www.fwa.fun) (Fake
World Assets — on-chain NFT gacha protocol, Ethereum mainnet).

Target wallet: `0x5984bb82F11171cb1DC2287E2A6935c44D491538`

## Why this is a script and not a finished report

The session this was written in has an egress policy that blocks every
blockchain data source — Etherscan, Blockscout, Routescan, all public RPC
endpoints, CoinGecko, OpenSea, Reservoir, and `fwa.fun` itself all return
`403` at the proxy. No wallet data could be read, so no real numbers were
produced. Run this from a machine with normal internet access to get them.

## Run it

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

## What it deliberately cannot value

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
