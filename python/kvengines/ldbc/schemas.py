"""LDBC-SNB Interactive result schemas.

Defines the expected output format for all LDBC-SNB Interactive queries
(IC1-IC14, IS1-IS7). Each result class is a dataclass with:
- Field definitions matching the LDBC specification
- __lt__() for sorting where the spec requires a specific order
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Organization:
    """Organization (university or company) in IC1."""

    name: str
    year: int
    place: str

    def __lt__(self, other: Organization) -> bool:
        if self.year != other.year:
            return self.year > other.year  # DESC
        if self.name != other.name:
            return self.name < other.name
        return self.place < other.place


# ── Interactive Complex Queries (IC1–IC14) ──────────────────────────────


@dataclass
class IC1Result:
    """Friends with certain name."""

    friend_id: int
    friend_last_name: str
    distance_from_person: int
    friend_birthday: int
    friend_creation_date: int
    friend_gender: str
    friend_browser_used: str
    friend_location_ip: str
    friend_emails: list[str]
    friend_languages: list[str]
    friend_city_name: str
    friend_universities: list[Organization]
    friend_companies: list[Organization]

    def __lt__(self, other: IC1Result) -> bool:
        if self.distance_from_person != other.distance_from_person:
            return self.distance_from_person < other.distance_from_person
        if self.friend_last_name != other.friend_last_name:
            return self.friend_last_name < other.friend_last_name
        return self.friend_id < other.friend_id


@dataclass
class IC2Result:
    """Recent messages from friends."""

    person_id: int
    person_first_name: str
    person_last_name: str
    message_id: int
    message_content: str
    message_creation_date: int

    def __lt__(self, other: IC2Result) -> bool:
        if self.message_creation_date != other.message_creation_date:
            return self.message_creation_date > other.message_creation_date  # DESC
        return self.message_id < other.message_id


@dataclass
class IC3Result:
    """Friends and friends of friends in countries."""

    person_id: int
    person_first_name: str
    person_last_name: str
    x_count: int
    y_count: int
    count: int

    def __lt__(self, other: IC3Result) -> bool:
        if self.count != other.count:
            return self.count > other.count  # DESC
        return self.person_id < other.person_id


@dataclass
class IC4Result:
    """New topics."""

    tag_name: str
    post_count: int

    def __lt__(self, other: IC4Result) -> bool:
        if self.post_count != other.post_count:
            return self.post_count > other.post_count  # DESC
        return self.tag_name < other.tag_name


@dataclass
class IC5Result:
    """New groups."""

    forum_title: str
    post_count: int
    forum_id: int

    def __lt__(self, other: IC5Result) -> bool:
        if self.post_count != other.post_count:
            return self.post_count > other.post_count  # DESC
        return self.forum_id < other.forum_id


@dataclass
class IC6Result:
    """Tag co-occurrence."""

    tag_name: str
    post_count: int

    def __lt__(self, other: IC6Result) -> bool:
        if self.post_count != other.post_count:
            return self.post_count > other.post_count  # DESC
        return self.tag_name < other.tag_name


@dataclass
class IC7Result:
    """Recent likers."""

    person_id: int
    person_first_name: str
    person_last_name: str
    like_creation_date: int
    message_id: int
    message_content: str
    minutes_latency: int
    is_new: bool

    def __lt__(self, other: IC7Result) -> bool:
        if self.like_creation_date != other.like_creation_date:
            return self.like_creation_date > other.like_creation_date  # DESC
        return self.person_id < other.person_id


@dataclass
class IC8Result:
    """Recent comments."""

    person_id: int
    person_first_name: str
    person_last_name: str
    comment_creation_date: int
    comment_id: int
    comment_content: str

    def __lt__(self, other: IC8Result) -> bool:
        if self.comment_creation_date != other.comment_creation_date:
            return self.comment_creation_date > other.comment_creation_date  # DESC
        return self.comment_id < other.comment_id


@dataclass
class IC9Result:
    """Recent messages from friends and friends of friends."""

    person_id: int
    person_first_name: str
    person_last_name: str
    message_id: int
    message_content: str
    message_creation_date: int

    def __lt__(self, other: IC9Result) -> bool:
        if self.message_creation_date != other.message_creation_date:
            return self.message_creation_date > other.message_creation_date  # DESC
        return self.message_id < other.message_id


@dataclass
class IC10Result:
    """Friend recommendation."""

    person_id: int
    person_first_name: str
    person_last_name: str
    common_interest_score: int
    person_gender: str
    person_city_name: str

    def __lt__(self, other: IC10Result) -> bool:
        if self.common_interest_score != other.common_interest_score:
            return self.common_interest_score > other.common_interest_score  # DESC
        return self.person_id < other.person_id


@dataclass
class IC11Result:
    """Job referral."""

    person_id: int
    person_first_name: str
    person_last_name: str
    organization_name: str
    organization_work_from: int

    def __lt__(self, other: IC11Result) -> bool:
        if self.organization_work_from != other.organization_work_from:
            return self.organization_work_from < other.organization_work_from
        if self.person_id != other.person_id:
            return self.person_id < other.person_id
        return self.organization_name > other.organization_name  # DESC


@dataclass
class IC12Result:
    """Expert search."""

    person_id: int
    person_first_name: str
    person_last_name: str
    tag_names: list[str]
    reply_count: int

    def __lt__(self, other: IC12Result) -> bool:
        if self.reply_count != other.reply_count:
            return self.reply_count > other.reply_count  # DESC
        return self.person_id < other.person_id


@dataclass
class IC13Result:
    """Shortest path."""

    shortest_path_length: int


@dataclass
class IC14Result:
    """Weighted shortest paths."""

    person_ids_in_path: list[int]
    path_weight: float

    def __lt__(self, other: IC14Result) -> bool:
        return self.path_weight < other.path_weight


# ── Interactive Short Queries (IS1–IS7) ─────────────────────────────────


@dataclass
class IS1Result:
    """Person profile."""

    first_name: str
    last_name: str
    birthday: int
    location_ip: str
    browser_used: str
    city_id: int
    gender: str
    creation_date: int


@dataclass
class IS2Result:
    """Recent messages by person."""

    message_id: int
    message_content: str
    message_creation_date: int
    original_post_id: int
    original_post_author_id: int
    original_post_author_first_name: str
    original_post_author_last_name: str

    def __lt__(self, other: IS2Result) -> bool:
        if self.message_creation_date != other.message_creation_date:
            return self.message_creation_date > other.message_creation_date  # DESC
        return self.message_id < other.message_id


@dataclass
class IS3Result:
    """Friends of person."""

    person_id: int
    first_name: str
    last_name: str
    friendship_creation_date: int

    def __lt__(self, other: IS3Result) -> bool:
        if self.friendship_creation_date != other.friendship_creation_date:
            return self.friendship_creation_date > other.friendship_creation_date  # DESC
        return self.person_id < other.person_id


@dataclass
class IS4Result:
    """Message content."""

    message_creation_date: int
    message_content: str


@dataclass
class IS5Result:
    """Message creator."""

    person_id: int
    first_name: str
    last_name: str


@dataclass
class IS6Result:
    """Message forum."""

    forum_id: int
    forum_title: str
    moderator_id: int
    moderator_first_name: str
    moderator_last_name: str


@dataclass
class IS7Result:
    """Message replies."""

    comment_id: int
    comment_content: str
    comment_creation_date: int
    reply_author_id: int
    reply_author_first_name: str
    reply_author_last_name: str
    reply_author_knows_original_message_author: bool

    def __lt__(self, other: IS7Result) -> bool:
        if self.comment_creation_date != other.comment_creation_date:
            return self.comment_creation_date > other.comment_creation_date  # DESC
        return self.comment_id < other.comment_id
