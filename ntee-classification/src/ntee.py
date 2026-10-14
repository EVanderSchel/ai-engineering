"""The NTEE (National Taxonomy of Exempt Entities) categories this project predicts.

An NTEE code such as "B25" (secondary schools) or "X20" (Christian) starts with a letter: its major group.
The 26 groups roll up into 10 broad categories. Group Z ("Unknown, Unclassified") marks organizations
nobody has categorized, not a kind of organization, so it isn't a class here (see build_dataset.py).

Names follow the NCCS/IRS NTEE major group titles.
"""

GROUPS = {
    "A": "Arts, Culture and Humanities",
    "B": "Education",
    "C": "Environment",
    "D": "Animal-Related",
    "E": "Health Care",
    "F": "Mental Health and Crisis Intervention",
    "G": "Diseases, Disorders and Medical Disciplines",
    "H": "Medical Research",
    "I": "Crime and Legal-Related",
    "J": "Employment",
    "K": "Food, Agriculture and Nutrition",
    "L": "Housing and Shelter",
    "M": "Public Safety, Disaster Preparedness and Relief",
    "N": "Recreation and Sports",
    "O": "Youth Development",
    "P": "Human Services",
    "Q": "International, Foreign Affairs and National Security",
    "R": "Civil Rights, Social Action and Advocacy",
    "S": "Community Improvement and Capacity Building",
    "T": "Philanthropy, Voluntarism and Grantmaking Foundations",
    "U": "Science and Technology",
    "V": "Social Science",
    "W": "Public and Societal Benefit",
    "X": "Religion-Related",
    "Y": "Mutual and Membership Benefit",
}
UNKNOWN_GROUP = "Z"  # "Unknown, Unclassified": excluded

BROAD = {
    "I": ("Arts, Culture and Humanities", "A"),
    "II": ("Education", "B"),
    "III": ("Environment and Animals", "CD"),
    "IV": ("Health", "EFGH"),
    "V": ("Human Services", "IJKLMNOP"),
    "VI": ("International, Foreign Affairs", "Q"),
    "VII": ("Public, Societal Benefit", "RSTUVW"),
    "VIII": ("Religion Related", "X"),
    "IX": ("Mutual/Membership Benefit", "Y"),
}
_GROUP_TO_BROAD = {group: broad for broad, (_, groups) in BROAD.items() for group in groups}


def major_group(ntee_code: str) -> str | None:
    """The major group of an NTEE code ("B25" -> "B"), or None for a blank, malformed or unknown (Z) code."""
    code = (ntee_code or "").strip().upper()
    if not code or code[0] not in GROUPS:
        return None
    return code[0]


def broad_category(group: str) -> str:
    """The broad category (Roman numeral) a major group rolls up into ("P" -> "V")."""
    return _GROUP_TO_BROAD[group]
