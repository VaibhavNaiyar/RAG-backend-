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


def test_every_scoped_document_contributes_its_best_passage(settings):
    # The margin cut kept only the strongest document; a claim review lost brand_rules entirely
    # when the question's wording ("doctor-designed") scored weakly against every rule.
    tight = settings.with_(rerank_min_keep=1)  # one dominant document, as on the real corpus
    hits, _ = Retriever(tight).search("oral hormonal contraceptives", ["oral contraceptives barrier method"],
                                      ["zepbound_label", "brand_rules"], allowed=["zepbound_label", "brand_rules"])
    assert {h.chunk.doc_id for h in hits} == {"zepbound_label", "brand_rules"}
