# Citation recall: does the pipeline find the art an examiner found?

The strongest available ground truth for a prior-art search is the citation list an examiner
actually raised against a patent. These two tools measure the pipeline against one, end to end.

    .venv/bin/python eval/gold_probe.py DE3724659A1,US3240525A,...
        Is each cited publication in the corpus at all, under which spelling, and how much text do
        we hold for it? Run this FIRST: a recall number means nothing until you know the ceiling.

    .venv/bin/python eval/citation_recall.py <report-slug> DE3724659A1,US3240525A,...
        For a finished report, how far did each cited FAMILY get:
        not in corpus -> never retrieved -> retrieved -> screened -> read in full -> displayed.

Both take the citation list as an argument on purpose. A ruler with the answer written on it
measures nothing, and no part of the pipeline may ever be tuned against a specific list.

Two things to hold in mind when reading a number out of these:

  * **Count families, not publications.** The report shows one card per DOCDB simple family, so a
    citation is satisfied by any member of its family, and two cited documents in one family are
    one result.
  * **Exclude the subject's own family.** It is not prior art against itself, and it will otherwise
    flatter the score.

`CITATION_RECALL_2026-08-03.md` is the first study run with these, including the run-to-run
variance you should expect (roughly ±2 families).
