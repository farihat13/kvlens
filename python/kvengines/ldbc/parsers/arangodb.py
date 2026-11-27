"""ArangoDB LDBC result parser.

Converts raw AQL query results (list of dicts) into LDBC schema dataclasses.
AQL results use camelCase field names; this parser maps them to the snake_case
fields in kvengines.ldbc.schemas.

Simpler than the Dgraph/Nebula parsers because AQL returns flat dicts directly —
no @normalize flattening (Dgraph) or DataFrame conversion (Nebula) needed.
"""

from __future__ import annotations

from typing import Any

from kvengines.ldbc.parsers import LDBCParser
from kvengines.ldbc.schemas import (
    IC1Result, IC2Result, IC3Result, IC4Result, IC5Result, IC6Result,
    IC7Result, IC8Result, IC9Result, IC10Result, IC11Result, IC12Result,
    IC13Result, IC14Result,
    IS1Result, IS2Result, IS3Result, IS4Result, IS5Result, IS6Result, IS7Result,
    Organization,
)
from kvengines.utils import parse_int_safe, normalize_list


def _int(val: Any) -> int:
    """Safely coerce to int."""
    return parse_int_safe(val)


def _str(val: Any) -> str:
    """Safely coerce to str."""
    if val is None:
        return ""
    return str(val)


class ArangoDBLDBCParser(LDBCParser):
    """Parser for ArangoDB AQL query results.

    Raw input for each query is a list of dicts from the python-arango cursor.
    Field names match the RETURN clause in the AQL query files.
    """

    def parse(self, query_type: str, query_num: int, raw: Any) -> list:
        return self._dispatch(query_type, query_num, raw)

    # ── IC queries ──────────────────────────────────────────────────────

    def _parse_ic1(self, data: list[dict]) -> list[IC1Result]:
        results = []
        for row in data:
            unis = []
            for u in row.get("friendUniversities", []):
                if isinstance(u, (list, tuple)) and len(u) >= 3:
                    unis.append(Organization(name=_str(u[0]), year=_int(u[1]), place=_str(u[2])))
            unis.sort()

            comps = []
            for c in row.get("friendCompanies", []):
                if isinstance(c, (list, tuple)) and len(c) >= 3:
                    comps.append(Organization(name=_str(c[0]), year=_int(c[1]), place=_str(c[2])))
            comps.sort()

            results.append(IC1Result(
                friend_id=_int(row.get("friendId")),
                friend_last_name=_str(row.get("friendLastName")),
                distance_from_person=_int(row.get("distanceFromPerson")),
                friend_birthday=_int(row.get("friendBirthday")),
                friend_creation_date=_int(row.get("friendCreationDate")),
                friend_gender=_str(row.get("friendGender")),
                friend_browser_used=_str(row.get("friendBrowserUsed")),
                friend_location_ip=_str(row.get("friendLocationIp")),
                friend_emails=sorted(normalize_list(row.get("friendEmails", []))),
                friend_languages=sorted(normalize_list(row.get("friendLanguages", []))),
                friend_city_name=_str(row.get("friendCityName")),
                friend_universities=unis,
                friend_companies=comps,
            ))
        return sorted(results)

    def _parse_ic2(self, data: list[dict]) -> list[IC2Result]:
        results = []
        for row in data:
            results.append(IC2Result(
                person_id=_int(row.get("personId")),
                person_first_name=_str(row.get("personFirstName")),
                person_last_name=_str(row.get("personLastName")),
                message_id=_int(row.get("messageId")),
                message_content=_str(row.get("messageContent")),
                message_creation_date=_int(row.get("messageCreationDate")),
            ))
        return sorted(results)

    def _parse_ic3(self, data: list[dict]) -> list[IC3Result]:
        results = []
        for row in data:
            results.append(IC3Result(
                person_id=_int(row.get("personId")),
                person_first_name=_str(row.get("personFirstName")),
                person_last_name=_str(row.get("personLastName")),
                x_count=_int(row.get("xCount")),
                y_count=_int(row.get("yCount")),
                count=_int(row.get("count")),
            ))
        return sorted(results)

    def _parse_ic4(self, data: list[dict]) -> list[IC4Result]:
        results = []
        for row in data:
            results.append(IC4Result(
                tag_name=_str(row.get("tagName")),
                post_count=_int(row.get("postCount")),
            ))
        return sorted(results)

    def _parse_ic5(self, data: list[dict]) -> list[IC5Result]:
        results = []
        for row in data:
            results.append(IC5Result(
                forum_title=_str(row.get("forumTitle")),
                post_count=_int(row.get("postCount")),
                forum_id=_int(row.get("forumId")),
            ))
        return sorted(results)

    def _parse_ic6(self, data: list[dict]) -> list[IC6Result]:
        results = []
        for row in data:
            results.append(IC6Result(
                tag_name=_str(row.get("tagName")),
                post_count=_int(row.get("postCount")),
            ))
        return sorted(results)

    def _parse_ic7(self, data: list[dict]) -> list[IC7Result]:
        results = []
        for row in data:
            results.append(IC7Result(
                person_id=_int(row.get("personId")),
                person_first_name=_str(row.get("personFirstName")),
                person_last_name=_str(row.get("personLastName")),
                like_creation_date=_int(row.get("likeCreationDate")),
                message_id=_int(row.get("messageId")),
                message_content=_str(row.get("messageContent")),
                minutes_latency=_int(row.get("minutesLatency")),
                is_new=bool(row.get("isNew")),
            ))
        return sorted(results)

    def _parse_ic8(self, data: list[dict]) -> list[IC8Result]:
        results = []
        for row in data:
            results.append(IC8Result(
                person_id=_int(row.get("personId")),
                person_first_name=_str(row.get("personFirstName")),
                person_last_name=_str(row.get("personLastName")),
                comment_creation_date=_int(row.get("commentCreationDate")),
                comment_id=_int(row.get("commentId")),
                comment_content=_str(row.get("commentContent")),
            ))
        return sorted(results)

    def _parse_ic9(self, data: list[dict]) -> list[IC9Result]:
        results = []
        for row in data:
            results.append(IC9Result(
                person_id=_int(row.get("personId")),
                person_first_name=_str(row.get("personFirstName")),
                person_last_name=_str(row.get("personLastName")),
                message_id=_int(row.get("messageId")),
                message_content=_str(row.get("messageContent")),
                message_creation_date=_int(row.get("messageCreationDate")),
            ))
        return sorted(results)

    def _parse_ic10(self, data: list[dict]) -> list[IC10Result]:
        results = []
        for row in data:
            results.append(IC10Result(
                person_id=_int(row.get("personId")),
                person_first_name=_str(row.get("personFirstName")),
                person_last_name=_str(row.get("personLastName")),
                common_interest_score=_int(row.get("commonInterestScore")),
                person_gender=_str(row.get("personGender")),
                person_city_name=_str(row.get("personCityName")),
            ))
        return sorted(results)

    def _parse_ic11(self, data: list[dict]) -> list[IC11Result]:
        results = []
        for row in data:
            results.append(IC11Result(
                person_id=_int(row.get("personId")),
                person_first_name=_str(row.get("personFirstName")),
                person_last_name=_str(row.get("personLastName")),
                organization_name=_str(row.get("organizationName")),
                organization_work_from=_int(row.get("organizationWorkFrom")),
            ))
        return sorted(results)

    def _parse_ic12(self, data: list[dict]) -> list[IC12Result]:
        results = []
        for row in data:
            tag_names = row.get("tagNames", [])
            if isinstance(tag_names, list):
                tag_names = sorted([_str(t) for t in tag_names])
            else:
                tag_names = []
            results.append(IC12Result(
                person_id=_int(row.get("personId")),
                person_first_name=_str(row.get("personFirstName")),
                person_last_name=_str(row.get("personLastName")),
                tag_names=tag_names,
                reply_count=_int(row.get("replyCount")),
            ))
        return sorted(results)

    def _parse_ic13(self, data: list[dict]) -> list[IC13Result]:
        if not data:
            return [IC13Result(shortest_path_length=-1)]
        row = data[0]
        return [IC13Result(shortest_path_length=_int(row.get("shortestPathLength")))]

    def _parse_ic14(self, data: list[dict]) -> list[IC14Result]:
        results = []
        for row in data:
            person_ids = row.get("personIdsInPath", [])
            if isinstance(person_ids, list):
                person_ids = [_int(pid) for pid in person_ids]
            results.append(IC14Result(
                person_ids_in_path=person_ids,
                path_weight=float(row.get("pathWeight", 0)),
            ))
        return sorted(results)

    # ── IS queries ──────────────────────────────────────────────────────

    def _parse_is1(self, data: list[dict]) -> list[IS1Result]:
        if not data:
            return []
        row = data[0]
        return [IS1Result(
            first_name=_str(row.get("firstName")),
            last_name=_str(row.get("lastName")),
            birthday=_int(row.get("birthday")),
            location_ip=_str(row.get("locationIP")),
            browser_used=_str(row.get("browserUsed")),
            city_id=_int(row.get("cityId")),
            gender=_str(row.get("gender")),
            creation_date=_int(row.get("creationDate")),
        )]

    def _parse_is2(self, data: list[dict]) -> list[IS2Result]:
        results = []
        for row in data:
            results.append(IS2Result(
                message_id=_int(row.get("messageId")),
                message_content=_str(row.get("messageContent")),
                message_creation_date=_int(row.get("messageCreationDate")),
                original_post_id=_int(row.get("originalPostId")),
                original_post_author_id=_int(row.get("originalPostAuthorId")),
                original_post_author_first_name=_str(row.get("originalPostAuthorFirstName")),
                original_post_author_last_name=_str(row.get("originalPostAuthorLastName")),
            ))
        return sorted(results)

    def _parse_is3(self, data: list[dict]) -> list[IS3Result]:
        results = []
        for row in data:
            results.append(IS3Result(
                person_id=_int(row.get("friendId")),
                first_name=_str(row.get("friendFirstName")),
                last_name=_str(row.get("friendLastName")),
                friendship_creation_date=_int(row.get("friendshipCreationDate")),
            ))
        return sorted(results)

    def _parse_is4(self, data: list[dict]) -> list[IS4Result]:
        if not data:
            return []
        row = data[0]
        return [IS4Result(
            message_creation_date=_int(row.get("messageCreationDate")),
            message_content=_str(row.get("messageContent")),
        )]

    def _parse_is5(self, data: list[dict]) -> list[IS5Result]:
        if not data:
            return []
        row = data[0]
        return [IS5Result(
            person_id=_int(row.get("personId")),
            first_name=_str(row.get("firstName")),
            last_name=_str(row.get("lastName")),
        )]

    def _parse_is6(self, data: list[dict]) -> list[IS6Result]:
        if not data:
            return []
        row = data[0]
        return [IS6Result(
            forum_id=_int(row.get("forumId")),
            forum_title=_str(row.get("forumTitle")),
            moderator_id=_int(row.get("moderatorId")),
            moderator_first_name=_str(row.get("moderatorFirstName")),
            moderator_last_name=_str(row.get("moderatorLastName")),
        )]

    def _parse_is7(self, data: list[dict]) -> list[IS7Result]:
        results = []
        for row in data:
            results.append(IS7Result(
                comment_id=_int(row.get("commentId")),
                comment_content=_str(row.get("commentContent")),
                comment_creation_date=_int(row.get("commentCreationDate")),
                reply_author_id=_int(row.get("replyAuthorId")),
                reply_author_first_name=_str(row.get("replyAuthorFirstName")),
                reply_author_last_name=_str(row.get("replyAuthorLastName")),
                reply_author_knows_original_message_author=bool(
                    row.get("replyAuthorKnowsOriginalMessageAuthor")
                ),
            ))
        return sorted(results)
