"""Chunking on page text shaped like the real corpus (pypdf output of the labels and FTC guide)."""
from claimcheck.ingest import _drop_contents, _strip_running, chunk_document

HDR = "DRUGX (drugide) injection"
LABEL = [
    (1, "HIGHLIGHTS OF PRESCRIBING INFORMATION\nThese highlights do not include all the information needed to use DRUGX.\n"
        "WARNING: RISK OF THYROID C−CELL TUMORS\nSee full prescribing information for complete boxed warning.\n"
        "• In rodents, drugx causes thyroid C-cell tumors at clinically relevant exposures.\n"
        "——— INDICATIONS AND USAGE ———\nDRUGX is indicated to reduce excess body weight in adults with obesity."),
    (2, f"{HDR}\n2\nFULL PRESCRIBING INFORMATION: CONTENTS*\nWARNING: RISK OF THYROID C−CELL TUMORS\n"
        "1 INDICATIONS AND USAGE\n2 DOSAGE AND ADMINISTRATION\n 2.1 Recommended Dosage\n"
        "5 WARNINGS AND PRECAUTIONS\n 5.1 Risk of Thyroid C-Cell Tumors"),
    (3, f"{HDR}\n3\nFULL PRESCRIBING INFORMATION\nWARNING: RISK OF THYROID C−CELL TUMORS\n"
        "• In rodents, drugx causes dose-dependent thyroid C-cell tumors. It is unknown whether DRUGX causes MTC.\n"
        "1 INDICATIONS AND USAGE\nDRUGX is indicated in combination with a reduced-calorie diet and exercise.\n"
        "2 DOSAGE AND ADMINISTRATION\n2.1 Switching Between DRUGX Injection and\nDRUGX Tablets\n"
        "The maintenance dosage is 2.4 mg injected subcutaneously once weekly.\n"
        "2.2 Weight Reduction and Long-term Maintenance Study of DRUGX Injection in Pediatric Patients Aged 12\n"
        "Pediatric patients aged 12 years and older lost more weight on DRUGX than on placebo."),
    (4, f"{HDR}\n4\n4 CONTRAINDICATIONS\nDRUGX is contraindicated in patients with a personal or family history of MTC.\n"
        "5 WARNINGS AND PRECAUTIONS\n5.1 Risk of Thyroid C-Cell Tumors\n"
        "Counsel patients regarding the potential risk for MTC and the symptoms of thyroid tumors."),
    (5, f"{HDR} Medication Guide\n5\nWhat is the most important information I should know about DRUGX?\n"
        "DRUGX may cause serious side effects, including possible thyroid tumors."),
]

FTC = [(n, f"Health Products Compliance Guidance • {n}\n{body}") for n, body in [
    (1, "I. Preface\nFTC staff has prepared this guidance document to update and replace the 1998 guide.\n"
        "II. Overview of Regulatory Framework\nA. FTC Authority over Advertising of Health-Related Products\n"
        "1. Advertising must be truthful and not misleading; and\nThe FTC Act prohibits unfair or deceptive acts."),
    (2, "B. Substantiating Claims\nAdvertisers must have competent and reliable scientific evidence for claims.\n"
        "4. THE TOTALITY OF THE EVIDENCE\nStudies cannot be evaluated in isolation from the surrounding evidence."),
    (3, "C. Other Advertising Issues\n4. MISCHARACTERIZATIONS OF FDA APPROVAL\n"
        "Marketers should not suggest that the FDA has approved a product that it has not approved.\n"
        "III. Conclusion\nThe FTC will continue to monitor health claims and bring enforcement actions.\n"
        "Endnotes\n1 Section 5 of the FTC Act prohibits unfair or deceptive acts or practices in commerce."),
]]


def _chunks(pages, kind):
    doc = {"id": "d", "title": "D", "kind": kind}
    return chunk_document(doc, _drop_contents(_strip_running(pages)), 900, 150)


def _section_of(chunks, phrase):
    (c,) = [c for c in chunks if phrase in c.text]
    return c.page, c.section


def test_label_sections_boxed_warning_and_page_numbers():
    cs = _chunks(LABEL, "fda_label")
    assert _section_of(cs, "In rodents, drugx causes thyroid") == (1, "Warning: Risk Of Thyroid C-Cell Tumors")
    assert _section_of(cs, "reduce excess body weight") == (1, "Highlights: Indications And Usage")
    assert _section_of(cs, "dose-dependent thyroid") == (3, "Warning: Risk Of Thyroid C-Cell Tumors")
    assert _section_of(cs, "reduced-calorie diet") == (3, "1 Indications And Usage")
    assert _section_of(cs, "2.4 mg") == (3, "2.1 Switching Between DRUGX Injection and DRUGX Tablets")
    assert _section_of(cs, "lost more weight")[1].startswith("2.2 Weight Reduction and Long-term Maintenance Study")
    assert _section_of(cs, "contraindicated") == (4, "4 Contraindications")
    assert _section_of(cs, "Counsel patients") == (4, "5.1 Risk of Thyroid C-Cell Tumors")
    assert _section_of(cs, "possible thyroid tumors") == (5, "Medication Guide")


def test_label_contents_page_and_running_headers_are_not_indexed():
    cs = _chunks(LABEL, "fda_label")
    assert not [c for c in cs if c.page == 2]
    assert not [c for c in cs if HDR in c.text or "CONTENTS" in c.text]


def test_ftc_outline_becomes_nested_sections():
    cs = _chunks(FTC, "ftc_guidance")
    assert _section_of(cs, "1998 guide") == (1, "I. Preface")
    assert _section_of(cs, "The FTC Act prohibits") == (1, "II.A. FTC Authority over Advertising of Health-Related Products")
    assert _section_of(cs, "isolation") == (2, "II.B.4. The totality of the evidence")
    assert _section_of(cs, "has approved a product") == (3, "II.C.4. Mischaracterizations of FDA approval")
    assert _section_of(cs, "enforcement actions") == (3, "III. Conclusion")
    assert _section_of(cs, "Section 5 of the FTC Act") == (3, "Endnotes")
    assert not [c for c in cs if "Compliance Guidance •" in c.text]
