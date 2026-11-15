"""Seat letters in the chairman's text become seat names, but document references keep their letter."""
import pytest

from committee.followup import name_labels

NAMES = {"A": "Home Inspector", "B": "Money Coach", "C": "Tenant Lawyer", "D": "Devil's Advocate"}


@pytest.mark.parametrize("text", [
    "Obtain Elm's full lease package: Exhibit B community rules and all addenda.",
    "Read Schedule C before signing.",
    "If that fails, go to Plan B.",
    "Section D of the lease covers repairs.",
    "See Appendix B, Annex C and Form D.",
    "Exhibits B and the fee schedule are missing.",
    "Unit C is on the third floor.",
])
def test_document_references_keep_their_letter(text):
    assert name_labels(text, NAMES) == text


@pytest.mark.parametrize("text, want", [
    ("B and C agree; D dissents.", "Money Coach and Tenant Lawyer agree; Devil's Advocate dissents."),
    ("Response A over-diagnosed the photo.", "Home Inspector over-diagnosed the photo."),
    ("C's quote of §14.2 settles it.", "Tenant Lawyer's quote of §14.2 settles it."),
    ("As B showed, Exhibit B is missing.", "As Money Coach showed, Exhibit B is missing."),
])
def test_seat_labels_still_become_names(text, want):
    assert name_labels(text, NAMES) == want


def test_a_bare_a_stays_the_article():
    assert name_labels("A lease is a contract.", NAMES) == "A lease is a contract."
