"""Offline tests for the deterministic datasheet layer -- no network, no
LLM. Fetching and digesting are exercised live (they hit the internet and
the API by design); what's tested here is everything that decides whether a
fact is allowed to exist: quote verification, slug candidates, page-tagged
digest text.
"""

from schemantic.datasheets import (
    _ti_candidate_slugs,
    digest_text,
    verify_quote,
)

PAGES = [
    "INA219 Zero-Drift, Bidirectional Current/Power Monitor\nSenses bus voltages from 0 to 26 V",
    "SUPPLY VOLTAGE: 3 to 5.5 V\nI2C- AND SMBUS-COMPATIBLE INTERFACE",
]


def test_verified_quote_passes():
    assert verify_quote("Senses bus voltages from 0 to 26 V", PAGES, 1)


def test_quote_on_wrong_page_fails():
    assert not verify_quote("Senses bus voltages from 0 to 26 V", PAGES, 2)


def test_paraphrased_quote_fails():
    # "measures" instead of "senses" -- close, plausible, and exactly the
    # kind of drift that must be dropped, not displayed
    assert not verify_quote("Measures bus voltages from 0 to 26 V", PAGES, 1)


def test_whitespace_and_case_normalization():
    assert verify_quote("  senses BUS  voltages\nfrom 0 to 26 v ", PAGES, 1)


def test_out_of_range_page_fails():
    assert not verify_quote("anything", PAGES, 0)
    assert not verify_quote("anything", PAGES, 99)


def test_ti_slug_candidates_most_specific_first():
    slugs = _ti_candidate_slugs("INA219BIDR")
    assert slugs[0] == "ina219bidr"
    assert "ina219" in slugs  # the family name TI's symlink actually resolves


def test_digest_text_tags_pages_one_indexed():
    text = digest_text(PAGES)
    assert "=== PAGE 1 ===" in text
    assert "=== PAGE 2 ===" in text
    assert text.index("PAGE 1") < text.index("PAGE 2")
