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


def test_a_passage_only_a_sub_query_targets_survives_the_rerank(settings):
    # The planner's sub-queries name what the question does not ("randomized clinical trials");
    # reranking against the question alone cut exactly those passages (FTC-01).
    tight = settings.with_(rerank_min_keep=1, rerank_margin=0.0)
    hits, _ = Retriever(tight).search("What level of evidence does the FTC expect?", ["testimonials typical results"],
                                      ["ftc_health_claims"], allowed=["ftc_health_claims"])
    assert "ftc_health_claims:p1:2" in {h.chunk.id for h in hits}


def test_a_scoped_document_outside_the_candidate_pool_still_contributes(settings):
    # The floor only looked inside the pool; a claim review whose wording missed every brand rule
    # got no brand_rules passage at all (CLAIM-06).
    small = settings.with_(candidate_k=2, rerank_min_keep=1)
    hits, _ = Retriever(small).search("competent and reliable scientific evidence", ["competent and reliable scientific evidence"],
                                      ["ftc_health_claims", "brand_rules"], allowed=["ftc_health_claims", "brand_rules"])
    assert "brand_rules" in {h.chunk.doc_id for h in hits}


def test_the_planner_catalogue_lists_each_documents_headings(settings):
    # The planner writes the sub-queries; it can only use the corpus's words if it sees them.
    # FTC never says "establishment claim": its heading is "a specific level of support" (FTC-02).
    cat = {d["doc_id"]: d for d in Retriever(settings).index.catalogue()}
    assert "Establishment claims" in cat["ftc_health_claims"]["sections"]
    assert len(cat["brand_rules"]["sections"]) == len(set(cat["brand_rules"]["sections"]))
