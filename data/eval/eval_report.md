# Pilot Evaluation — 5-config ablation (spec §8)
_Generated 2026-07-17 · 11 frozen gold searches · corpus 107,795 pubs / 1.82M vectors_

## Mean family recall@k (macro-avg over gold searches)

| Config | recall@100 | recall@500 | recall@1000 | reachable@100 | nongold@20 |
|---|--:|--:|--:|--:|--:|
| keyword | 0.0 | 0.0926 | 0.1075 | 0.0 | 1.0 |
| vector | 0.1749 | 0.2427 | 0.3023 | 0.1815 | 0.9864 |
| hybrid | 0.0905 | 0.2011 | 0.2546 | 0.0965 | 0.9909 |
| hybrid_rerank | 0.0905 | 0.2011 | 0.2525 | 0.0965 | 0.9864 |
| agentic | 0.0673 | 0.1979 | 0.2373 | 0.0751 | 0.9909 |

## Earliest-relevant-publication recovered (count yes / total)

- keyword: 0/11
- vector: 3/11
- hybrid: 1/11
- hybrid_rerank: 1/11
- agentic: 2/11

## Unique-contribution findings

- `grabo_gripper_novelty`: agent-only gold families=['22883737', '24430975', '39244538', '41380067', '6332344'] · citation-only=['23289430', '39244538', '7961965']
- `grabo_gripper_inventive`: agent-only gold families=['23205091', '24840037', '34201690', '39244538', '41380067', '6332344', '6495403'] · citation-only=['34810482', '39244538']
- `grabo_extended_frame`: agent-only gold families=['22577009', '23158645', '23289430', '24840037', '29740151', '3193909', '31947993', '45508079', '6332344', '7865086'] · citation-only=['25479119', '25644276', '27070290', '32054112', '45508079', '7865086']
- `grabo_de_utility_xling`: agent-only gold families=[] · citation-only=['6495403']
- `schmalz_vacuum_clamp`: agent-only gold families=['26040986', '7790219'] · citation-only=['7790219']

## Per-query family recall@100

| query | cat | mode | gold | reach | keyword | vector | hybrid | +rerank | agentic |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|
| `grabo_gripper_novelty` | grabo_own | novelty | 44 | 37 | 0.0 | 0.0455 | 0.0227 | 0.0227 | 0.0682 |
| `grabo_gripper_inventive` | grabo_own | inventive_step | 44 | 37 | 0.0 | 0.0455 | 0.0227 | 0.0227 | 0.0682 |
| `grabo_extended_frame` | grabo_own | novelty | 49 | 45 | 0.0 | 0.0 | 0.0204 | 0.0204 | 0.0408 |
| `grabo_de_utility_xling` | cross_lingual | novelty | 7 | 7 | 0.0 | 0.1429 | 0.0 | 0.0 | 0.0 |
| `schmalz_sauggreifsystem` | competitor | novelty | 4 | 4 | 0.0 | 0.25 | 0.0 | 0.0 | 0.0 |
| `schmalz_vacuum_clamp` | competitor | inventive_step | 7 | 7 | 0.0 | 0.4286 | 0.2857 | 0.2857 | 0.2857 |
| `probst_stone_lifter_xling` | cross_lingual | novelty | 6 | 6 | 0.0 | 0.1667 | 0.0 | 0.0 | 0.0 |
| `probst_kerb_lifter` | competitor | inventive_step | 9 | 6 | 0.0 | 0.1111 | 0.1111 | 0.1111 | 0.1111 |
| `nl_handheld_vacuum_seal_sensor` | hard_combination | novelty | 5 | 5 | 0.0 | 0.4 | 0.2 | 0.2 | 0.0 |
| `nl_porous_surface_gripper` | hard_combination | novelty | 6 | 6 | 0.0 | 0.3333 | 0.3333 | 0.3333 | 0.1667 |
| `nl_robot_eoat_vacuum` | neighbouring_cpc | inventive_step | 3 | 3 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
