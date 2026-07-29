# Vendored Aftermath skills — pin

Source repository : https://github.com/AftermathFinance/skills
Branch            : `feat/v2-skills`
Commit            : `5b614db62dcd2e58f442e93661f608fe7b073c32`
Commit subject    : `feat: aftermath-api v3.0.0`
Skill version     : `aftermath-perpetuals` v3.0.0
Vendored on       : 2026-07-28

Contents under `skills/` are copied **verbatim and unedited** from that commit.
Do not edit them here — corrections belong upstream. Local deviations are
recorded in `README-DELTA.md` instead.

## Why vendored

The skills define the integration contract this CLI implements (isolated-margin
model, circuit breakers, kill switch, preview tagged-unions, ID discipline,
BigInt wire format). Pinning them by commit makes the next upstream sync a
readable diff rather than a re-read, and keeps the contract reviewable at the
exact revision the code was written against.

## Updating

1. Fetch the branch: `git fetch origin feat/v2-skills`
2. Copy the new `skills/` tree in, replacing this one wholesale.
3. Update the commit hash and date above.
4. Re-read `README-DELTA.md` — verify each recorded discrepancy is still real,
   and delete the ones upstream has fixed.
5. Run `bun test`; the host-guard test fails if any live-code file picks up a
   retired host from the new material.

The branch head moves. This pin does not, until deliberately advanced.
