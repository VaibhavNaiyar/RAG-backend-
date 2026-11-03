from claimcheck.retrieve import Retriever


def test_scoped_label_search_stays_on_that_drug(settings):
    hits, _ = Retriever(settings).search("maintenance dose", ["maintenance dosage once weekly"], ["wegovy_label"],
                                         allowed=["wegovy_label", "zepbound_label"])
    assert hits and {h.chunk.doc_id for h in hits} == {"wegovy_label"}


def test_refs_are_sequential_and_citations_carry_page_and_section(settings):
    hits, _ = Retriever(settings).search("establishment claims clinically proven", ["clinically proven"])
    assert [h.ref for h in hits] == [f"E{i}" for i in range(1, len(hits) + 1)]
    assert all(h.chunk.page >= 1 for h in hits)
    assert any("BR-4" in h.chunk.section for h in hits)


def test_hybrid_fuses_both_retrievers(settings):
    hits, _ = Retriever(settings).search("oral hormonal contraceptives", ["oral contraceptives barrier method"])
    top = hits[0]
    assert top.chunk.doc_id == "zepbound_label"
    assert top.bm25_rank is not None and top.vector_rank is not None
