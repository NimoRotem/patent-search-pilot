# Pilot Evaluation — 5-config ablation (spec §8)
_Generated 2026-07-18 · 11 frozen gold searches · corpus 107,795 pubs / 1.82M vectors_

## Mean family recall@k (macro-avg over gold searches)

| Config | recall@100 | recall@500 | recall@1000 | reachable@100 | nongold@20 |
|---|--:|--:|--:|--:|--:|
| keyword | 0.0 | 0.0926 | 0.1075 | 0.0 | 1.0 |
| vector | 0.1697 | 0.283 | 0.3079 | 0.1774 | 0.9636 |
| hybrid | 0.1697 | 0.2358 | 0.3097 | 0.1774 | 0.9636 |
| hybrid_rerank | 0.1697 | 0.2358 | 0.3097 | 0.1774 | 0.9727 |
| agentic | 0.1263 | 0.2323 | 0.3285 | 0.1334 | 0.9909 |

## Earliest-relevant-publication recovered (count yes / total)

- keyword: 0/11
- vector: 2/11
- hybrid: 2/11
- hybrid_rerank: 2/11
- agentic: 2/11

## Unique-contribution findings

- `grabo_gripper_novelty`: agent-only gold families=['32054112', '39244538', '6332344', '6495403'] · citation-only=['32054112', '34810482', '39244538']
- `grabo_gripper_inventive`: agent-only gold families=['24664421', '24840037', '32054112', '42629144', '45508079', '6332344', '6495403', '7782789', '7889498'] · citation-only=['24262135', '24664421', '34810482', '39244538', '45508079']
- `grabo_extended_frame`: agent-only gold families=['22883737', '25421211', '3193909', '31947993', '32054112', '34201690', '45508079', '45554511', '6332344', '6918853'] · citation-only=['24430975', '25421211', '27070290', '29740151', '45508079']
- `grabo_de_utility_xling`: agent-only gold families=['7889498'] · citation-only=['6495403']
- `schmalz_vacuum_clamp`: agent-only gold families=['26040986'] · citation-only=['7790219']
- `probst_kerb_lifter`: agent-only gold families=['6391811'] · citation-only=[]

## Per-query family recall@100

| query | cat | mode | gold | reach | keyword | vector | hybrid | +rerank | agentic |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|
| `grabo_gripper_novelty` | grabo_own | novelty | 44 | 37 | 0.0 | 0.0682 | 0.0682 | 0.0682 | 0.0455 |
| `grabo_gripper_inventive` | grabo_own | inventive_step | 44 | 37 | 0.0 | 0.0682 | 0.0682 | 0.0682 | 0.0455 |
| `grabo_extended_frame` | grabo_own | novelty | 49 | 45 | 0.0 | 0.0408 | 0.0408 | 0.0408 | 0.0612 |
| `grabo_de_utility_xling` | cross_lingual | novelty | 7 | 7 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| `schmalz_sauggreifsystem` | competitor | novelty | 4 | 4 | 0.0 | 0.25 | 0.25 | 0.25 | 0.25 |
| `schmalz_vacuum_clamp` | competitor | inventive_step | 7 | 7 | 0.0 | 0.4286 | 0.4286 | 0.4286 | 0.1429 |
| `probst_stone_lifter_xling` | cross_lingual | novelty | 6 | 6 | 0.0 | 0.1667 | 0.1667 | 0.1667 | 0.0 |
| `probst_kerb_lifter` | competitor | inventive_step | 9 | 6 | 0.0 | 0.1111 | 0.1111 | 0.1111 | 0.1111 |
| `nl_handheld_vacuum_seal_sensor` | hard_combination | novelty | 5 | 5 | 0.0 | 0.4 | 0.4 | 0.4 | 0.4 |
| `nl_porous_surface_gripper` | hard_combination | novelty | 6 | 6 | 0.0 | 0.3333 | 0.3333 | 0.3333 | 0.3333 |
| `nl_robot_eoat_vacuum` | neighbouring_cpc | inventive_step | 3 | 3 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
