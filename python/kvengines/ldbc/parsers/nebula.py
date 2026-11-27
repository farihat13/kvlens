"""NebulaGraph parsers for LDBC-SNB Interactive queries.

Normalizes NebulaGraph nGQL ResultSet responses into the standard LDBC
result schemas. Results are converted to pandas DataFrames via
``resp.as_data_frame()`` for uniform row access.
"""

from __future__ import annotations

from typing import Any

from kvengines.ldbc.parsers import LDBCParser
from kvengines.ldbc.schemas import (
    IC1Result, IC2Result, IC3Result, IC4Result, IC5Result,
    IC6Result, IC7Result, IC8Result, IC9Result, IC10Result,
    IC11Result, IC12Result, IC13Result, IC14Result,
    IS1Result, IS2Result, IS3Result, IS4Result,
    IS5Result, IS6Result, IS7Result,
    Organization,
)
from kvengines.utils import normalize_list, parse_float_safe, parse_int_safe


class NebulaLDBCParser(LDBCParser):
    """Parser for NebulaGraph LDBC query responses.

    Each ``_parse_icN`` / ``_parse_isN`` method handles one query type.
    The ``raw`` argument is a NebulaGraph ``ResultSet`` object.

    Date handling: NebulaGraph returns dates as milliseconds-since-epoch
    integers (e.g. ``1352764800000``). Parsers convert via ``parse_int_safe()``
    which already returns ``int``, matching the LDBC integer format directly.

    Field-name divergences from LDBC schema (Nebula key → schema field):
        IC3:  personId → person_id, firstName → person_first_name,
              lastName → person_last_name, messagesInX → x_count,
              messagesInY → y_count
        IC5:  forumTitle → forum_title
        IC9:  friendId → person_id, messageId → message_id,
              messageContent → message_content,
              messageCreationDate → message_creation_date
        IC11: friendId → person_id, organizationName → organization_name,
              organizationWorkFromYear → organization_work_from
        IC14: personIdsInPath → person_ids_in_path
    """

    def parse(self, query_type: str, query_num: int, raw: Any) -> list:
        return self._dispatch(query_type, query_num, raw)

    # ── IC queries ──────────────────────────────────────────────────────

    def _parse_ic1(self, resp: Any) -> list[IC1Result]:
        results: list[IC1Result] = []
        df = resp.as_data_frame()

        for _, row in df.iterrows():
            universities: list[Organization] = []
            unis_data = row.get("friendUniversities", [])
            if isinstance(unis_data, list):
                for uni in unis_data:
                    if isinstance(uni, list) and len(uni) >= 3:
                        universities.append(Organization(
                            name=str(uni[0]), year=parse_int_safe(uni[1]), place=str(uni[2]),
                        ))
            universities.sort()

            companies: list[Organization] = []
            comps_data = row.get("friendCompanies", [])
            if isinstance(comps_data, list):
                for comp in comps_data:
                    if isinstance(comp, list) and len(comp) >= 3:
                        companies.append(Organization(
                            name=str(comp[0]), year=parse_int_safe(comp[1]), place=str(comp[2]),
                        ))
            companies.sort()

            results.append(IC1Result(
                friend_id=parse_int_safe(row.get("friendId")),
                friend_last_name=str(row.get("friendLastName", "")),
                distance_from_person=parse_int_safe(row.get("distanceFromPerson")),
                friend_birthday=parse_int_safe(row.get("friendBirthday")),
                friend_creation_date=parse_int_safe(row.get("friendCreationDate")),
                friend_gender=str(row.get("friendGender", "")),
                friend_browser_used=str(row.get("friendBrowserUsed", "")),
                friend_location_ip=str(row.get("friendLocationIp", "")),
                friend_emails=sorted(normalize_list(row.get("friendEmails", []))),
                friend_languages=sorted(normalize_list(row.get("friendLanguages", []))),
                friend_city_name=str(row.get("friendCityName", "")),
                friend_universities=universities,
                friend_companies=companies,
            ))

        return results

    def _parse_ic2(self, resp: Any) -> list[IC2Result]:
        results: list[IC2Result] = []
        df = resp.as_data_frame()

        for _, row in df.iterrows():
            results.append(IC2Result(
                person_id=parse_int_safe(row["friendId"]),
                person_first_name=str(row["friendFirstName"]),
                person_last_name=str(row["friendLastName"]),
                message_id=parse_int_safe(row["messageId"]),
                message_content=str(row.get("messageContent", "")),
                message_creation_date=parse_int_safe(row.get("messageCreationDate")),
            ))

        return results

    def _parse_ic3(self, resp: Any) -> list[IC3Result]:
        results: list[IC3Result] = []
        df = resp.as_data_frame()

        for _, row in df.iterrows():
            results.append(IC3Result(
                person_id=parse_int_safe(row.get("personId", "")),
                person_first_name=str(row.get("firstName", "")),
                person_last_name=str(row.get("lastName", "")),
                x_count=parse_int_safe(row.get("messagesInX")),
                y_count=parse_int_safe(row.get("messagesInY")),
                count=parse_int_safe(row.get("count")),
            ))

        return results

    def _parse_ic4(self, resp: Any) -> list[IC4Result]:
        df = resp.as_data_frame()
        return [
            IC4Result(
                tag_name=str(row.get("tagName", "")),
                post_count=parse_int_safe(row.get("postCount")),
            )
            for _, row in df.iterrows()
        ]

    def _parse_ic5(self, resp: Any) -> list[IC5Result]:
        df = resp.as_data_frame()
        return [
            IC5Result(
                forum_title=str(row.get("forumTitle", "")),
                post_count=parse_int_safe(row.get("postCount")),
                forum_id=parse_int_safe(row.get("forumId")),
            )
            for _, row in df.iterrows()
        ]

    def _parse_ic6(self, resp: Any) -> list[IC6Result]:
        df = resp.as_data_frame()
        return [
            IC6Result(
                tag_name=str(row.get("tagName", "")),
                post_count=parse_int_safe(row.get("postCount")),
            )
            for _, row in df.iterrows()
        ]

    def _parse_ic7(self, resp: Any) -> list[IC7Result]:
        df = resp.as_data_frame()
        return [
            IC7Result(
                person_id=parse_int_safe(row.get("personId")),
                person_first_name=str(row.get("personFirstName", "")),
                person_last_name=str(row.get("personLastName", "")),
                like_creation_date=parse_int_safe(row.get("likeCreationDate")),
                message_id=parse_int_safe(row.get("messageId")),
                message_content=str(row.get("messageContent", "")),
                minutes_latency=self._ic7_latency(row),
                is_new=bool(row.get("isNew", False)),
            )
            for _, row in df.iterrows()
        ]

    @staticmethod
    def _ic7_latency(row: Any) -> int:
        """minutesLatency, or -- when the query text returns the message's creationDate instead
        (the kvformat engine-fidelity IC7 text does) -- the client computes it: floor of the
        like-to-message gap in minutes, as LDBC defines it."""
        if row.get("minutesLatency") is not None:
            return parse_int_safe(row.get("minutesLatency"))
        like, msg = row.get("likeCreationDate"), row.get("messageCreationDate")
        if like is None or msg is None:
            return 0
        return (parse_int_safe(like) - parse_int_safe(msg)) // 60000

    def _parse_ic8(self, resp: Any) -> list[IC8Result]:
        df = resp.as_data_frame()
        return [
            IC8Result(
                person_id=parse_int_safe(row.get("personId")),
                person_first_name=str(row.get("personFirstName", "")),
                person_last_name=str(row.get("personLastName", "")),
                comment_creation_date=parse_int_safe(row.get("commentCreationDate")),
                comment_id=parse_int_safe(row.get("commentId")),
                comment_content=str(row.get("commentContent", "")),
            )
            for _, row in df.iterrows()
        ]

    def _parse_ic9(self, resp: Any) -> list[IC9Result]:
        df = resp.as_data_frame()
        return [
            IC9Result(
                person_id=parse_int_safe(row.get("friendId")),
                person_first_name=str(row.get("friendFirstName", "")),
                person_last_name=str(row.get("friendLastName", "")),
                message_id=parse_int_safe(row.get("messageId")),
                message_content=str(row.get("messageContent", "")),
                message_creation_date=parse_int_safe(row.get("messageCreationDate")),
            )
            for _, row in df.iterrows()
        ]

    def _parse_ic10(self, resp: Any) -> list[IC10Result]:
        df = resp.as_data_frame()
        return [
            IC10Result(
                person_id=parse_int_safe(row.get("personId")),
                person_first_name=str(row.get("personFirstName", "")),
                person_last_name=str(row.get("personLastName", "")),
                common_interest_score=parse_int_safe(row.get("commonInterestScore")),
                person_gender=str(row.get("personGender", "")),
                person_city_name=str(row.get("personCityName", "")),
            )
            for _, row in df.iterrows()
        ]

    def _parse_ic11(self, resp: Any) -> list[IC11Result]:
        df = resp.as_data_frame()
        return [
            IC11Result(
                person_id=parse_int_safe(row.get("friendId")),
                person_first_name=str(row.get("friendFirstName", "")),
                person_last_name=str(row.get("friendLastName", "")),
                organization_name=str(row.get("organizationName", "")),
                organization_work_from=parse_int_safe(row.get("organizationWorkFromYear")),
            )
            for _, row in df.iterrows()
        ]

    def _parse_ic12(self, resp: Any) -> list[IC12Result]:
        df = resp.as_data_frame()
        return [
            IC12Result(
                person_id=parse_int_safe(row.get("friendId")),
                person_first_name=str(row.get("friendFirstName", "")),
                person_last_name=str(row.get("friendLastName", "")),
                tag_names=normalize_list(row.get("tagNames", [])),
                reply_count=parse_int_safe(row.get("replyCount")),
            )
            for _, row in df.iterrows()
        ]

    def _parse_ic13(self, resp: Any) -> IC13Result:
        df = resp.as_data_frame()
        if len(df) == 0:
            return IC13Result(shortest_path_length=-1)
        return IC13Result(
            shortest_path_length=parse_int_safe(df.iloc[0].get("shortestPathLength", -1), default=-1),
        )

    def _parse_ic14(self, resp: Any) -> list[IC14Result]:
        df = resp.as_data_frame()
        return [
            IC14Result(
                person_ids_in_path=[parse_int_safe(pid) for pid in normalize_list(row.get("personIdsInPath", []))],
                path_weight=parse_float_safe(row.get("pathWeight")),
            )
            for _, row in df.iterrows()
        ]

    # ── IS queries ──────────────────────────────────────────────────────

    def _parse_is1(self, resp: Any) -> IS1Result:
        df = resp.as_data_frame()
        if len(df) == 0:
            return IS1Result(
                first_name="", last_name="", birthday=0, location_ip="",
                browser_used="", city_id=0, gender="", creation_date=0,
            )
        row = df.iloc[0]
        return IS1Result(
            first_name=str(row.get("firstName", "")),
            last_name=str(row.get("lastName", "")),
            birthday=parse_int_safe(row.get("birthday")),
            location_ip=str(row.get("locationIP", "")),
            browser_used=str(row.get("browserUsed", "")),
            city_id=parse_int_safe(row.get("cityId")),
            gender=str(row.get("gender", "")),
            creation_date=parse_int_safe(row.get("creationDate")),
        )

    def _parse_is2(self, resp: Any) -> list[IS2Result]:
        df = resp.as_data_frame()
        return [
            IS2Result(
                message_id=parse_int_safe(row.get("messageId")),
                message_content=str(row.get("messageContent", "")),
                message_creation_date=parse_int_safe(row.get("messageCreationDate")),
                original_post_id=parse_int_safe(row.get("originalPostId")),
                original_post_author_id=parse_int_safe(row.get("originalPostAuthorId")),
                original_post_author_first_name=str(row.get("originalPostAuthorFirstName", "")),
                original_post_author_last_name=str(row.get("originalPostAuthorLastName", "")),
            )
            for _, row in df.iterrows()
        ]

    def _parse_is3(self, resp: Any) -> list[IS3Result]:
        df = resp.as_data_frame()
        return [
            IS3Result(
                person_id=parse_int_safe(row.get("friendId")),
                first_name=str(row.get("friendFirstName", "")),
                last_name=str(row.get("friendLastName", "")),
                friendship_creation_date=parse_int_safe(row.get("friendshipCreationDate")),
            )
            for _, row in df.iterrows()
        ]

    def _parse_is4(self, resp: Any) -> IS4Result:
        df = resp.as_data_frame()
        if len(df) == 0:
            return IS4Result(message_creation_date=0, message_content="")
        row = df.iloc[0]
        return IS4Result(
            message_creation_date=parse_int_safe(row.get("messageCreationDate")),
            message_content=str(row.get("messageContent", "")),
        )

    def _parse_is5(self, resp: Any) -> IS5Result:
        df = resp.as_data_frame()
        if len(df) == 0:
            return IS5Result(person_id=0, first_name="", last_name="")
        row = df.iloc[0]
        return IS5Result(
            person_id=parse_int_safe(row.get("personId")),
            first_name=str(row.get("firstName", "")),
            last_name=str(row.get("lastName", "")),
        )

    def _parse_is6(self, resp: Any) -> IS6Result:
        df = resp.as_data_frame()
        if len(df) == 0:
            return IS6Result(
                forum_id=0, forum_title="",
                moderator_id=0, moderator_first_name="", moderator_last_name="",
            )
        row = df.iloc[0]
        return IS6Result(
            forum_id=parse_int_safe(row.get("forumId")),
            forum_title=str(row.get("forumTitle", "")),
            moderator_id=parse_int_safe(row.get("moderatorId")),
            moderator_first_name=str(row.get("moderatorFirstName", "")),
            moderator_last_name=str(row.get("moderatorLastName", "")),
        )

    def _parse_is7(self, resp: Any) -> list[IS7Result]:
        df = resp.as_data_frame()
        return [
            IS7Result(
                comment_id=parse_int_safe(row.get("commentId")),
                comment_content=str(row.get("commentContent", "")),
                comment_creation_date=parse_int_safe(row.get("commentCreationDate")),
                reply_author_id=parse_int_safe(row.get("replyAuthorId")),
                reply_author_first_name=str(row.get("replyAuthorFirstName", "")),
                reply_author_last_name=str(row.get("replyAuthorLastName", "")),
                reply_author_knows_original_message_author=bool(row.get("knows", False)),
            )
            for _, row in df.iterrows()
        ]
