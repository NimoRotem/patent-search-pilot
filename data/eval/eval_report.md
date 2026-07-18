# Pilot Evaluation — 5-config ablation (spec §8)
_Generated 2026-07-18 · 11 frozen gold searches · corpus 107,795 pubs / 1.82M vectors_

## Mean family recall@k (macro-avg over gold searches)

| Config | recall@100 | recall@500 | recall@1000 | reachable@100 | nongold@20 |
|---|--:|--:|--:|--:|--:|
| keyword | 0.0152 | 0.1056 | 0.1356 | 0.0152 | 1.0 |
| vector | 0.1697 | 0.2603 | 0.2833 | 0.1774 | 0.9636 |
| hybrid | 0.1697 | 0.2747 | 0.3111 | 0.1774 | 0.9636 |
| hybrid_rerank | 0.1697 | 0.2747 | 0.3111 | 0.1774 | 0.9773 |
| agentic | 0.1856 | 0.263 | 0.2921 | 0.1948 | 0.9773 |

## Earliest-relevant-publication recovered (count yes / total)

- keyword: 0/11
- vector: 1/11
- hybrid: 1/11
- hybrid_rerank: 1/11
- agentic: 2/11

## Unique-contribution findings

- `grabo_gripper_novelty`: agent-only gold families=['24664421', '24840037', '32054112', '39244538', '45508079', '6332344'] · citation-only=['23289430', '24262135', '24664421', '25421211', '32054112', '34810482', '39244538', '45508079', '45554511']
- `grabo_gripper_inventive`: agent-only gold families=['24262135', '24664421', '24840037', '32054112', '6332344'] · citation-only=['24664421', '29740151', '39244538']
- `grabo_extended_frame`: agent-only gold families=['25421211', '3193909', '31947993', '32054112', '45508079', '6332344'] · citation-only=['22883737', '24664421', '25421211', '25479119', '27070290', '45508079']
- `grabo_de_utility_xling`: agent-only gold families=['7889498'] · citation-only=[]
- `schmalz_sauggreifsystem`: agent-only gold families=['32297463'] · citation-only=[]
- `schmalz_vacuum_clamp`: agent-only gold families=['26040986'] · citation-only=['7790219']
- `probst_kerb_lifter`: agent-only gold families=['6391811'] · citation-only=[]

## Per-query family recall@100

| query | cat | mode | gold | reach | keyword | vector | hybrid | +rerank | agentic |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|
| `grabo_gripper_novelty` | grabo_own | novelty | 44 | 37 | 0.0 | 0.0682 | 0.0682 | 0.0682 | 0.0909 |
| `grabo_gripper_inventive` | grabo_own | inventive_step | 44 | 37 | 0.0 | 0.0682 | 0.0682 | 0.0682 | 0.1364 |
| `grabo_extended_frame` | grabo_own | novelty | 49 | 45 | 0.0 | 0.0408 | 0.0408 | 0.0408 | 0.0408 |
| `grabo_de_utility_xling` | cross_lingual | novelty | 7 | 7 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| `schmalz_sauggreifsystem` | competitor | novelty | 4 | 4 | 0.0 | 0.25 | 0.25 | 0.25 | 0.5 |
| `schmalz_vacuum_clamp` | competitor | inventive_step | 7 | 7 | 0.0 | 0.4286 | 0.4286 | 0.4286 | 0.4286 |
| `probst_stone_lifter_xling` | cross_lingual | novelty | 6 | 6 | 0.0 | 0.1667 | 0.1667 | 0.1667 | 0.0 |
| `probst_kerb_lifter` | competitor | inventive_step | 9 | 6 | 0.0 | 0.1111 | 0.1111 | 0.1111 | 0.1111 |
| `nl_handheld_vacuum_seal_sensor` | hard_combination | novelty | 5 | 5 | 0.0 | 0.4 | 0.4 | 0.4 | 0.4 |
| `nl_porous_surface_gripper` | hard_combination | novelty | 6 | 6 | 0.1667 | 0.3333 | 0.3333 | 0.3333 | 0.3333 |
| `nl_robot_eoat_vacuum` | neighbouring_cpc | inventive_step | 3 | 3 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
