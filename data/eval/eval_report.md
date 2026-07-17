# Pilot Evaluation — 5-config ablation (spec §8)
_Generated 2026-07-17 · 1 frozen gold searches · corpus 107,795 pubs / 1.82M vectors_

## Mean family recall@k (macro-avg over gold searches)

| Config | recall@100 | recall@500 | recall@1000 | reachable@100 | nongold@20 |
|---|--:|--:|--:|--:|--:|
| keyword | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 |
| vector | 0.0455 | 0.2045 | 0.2045 | 0.0541 | 1.0 |
| hybrid | 0.0227 | 0.1364 | 0.2045 | 0.027 | 1.0 |
| hybrid_rerank | 0.0227 | 0.1364 | 0.2045 | 0.027 | 1.0 |
| agentic | 0.0682 | 0.1364 | 0.25 | 0.0811 | 0.95 |

## Earliest-relevant-publication recovered (count yes / total)

- keyword: 0/1
- vector: 0/1
- hybrid: 0/1
- hybrid_rerank: 0/1
- agentic: 0/1

## Unique-contribution findings

- `grabo_gripper_novelty`: agent-only gold families=['22883737', '32054112', '39244538', '6332344'] · citation-only=['23289430', '32054112', '39244538', '45508079']

## Per-query family recall@100

| query | cat | mode | gold | reach | keyword | vector | hybrid | +rerank | agentic |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|
| `grabo_gripper_novelty` | grabo_own | novelty | 44 | 37 | 0.0 | 0.0455 | 0.0227 | 0.0227 | 0.0682 |
