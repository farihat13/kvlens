"""Dgraph parsers for LDBC-SNB Interactive queries.

Normalizes Dgraph's GraphQL/DQL JSON response format into the standard
LDBC result schemas. Some parsers include client-side aggregation, sorting,
or filtering due to Dgraph DQL limitations.
"""

from __future__ import annotations

import sys
from collections import OrderedDict, defaultdict
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
from kvengines.utils import (
    iso8601_to_ms, normalize_list, parse_dgraph_datetime,
    parse_float_safe, parse_int_safe,
)


# LDBC spec result limits per query (Section 3.3 of the spec).
# Nebula applies these server-side via LIMIT; Dgraph needs client-side truncation.
RESULT_LIMITS: dict[str, int] = {
    "ic1": 20, "ic3": 20, "ic4": 10, "ic5": 20, "ic6": 10,
    "ic7": 20, "ic8": 20, "ic9": 20, "ic10": 10, "ic11": 10,
}


class DgraphLDBCParser(LDBCParser):
    """Parser for Dgraph LDBC query responses.

    Each ``_parse_icN`` / ``_parse_isN`` method handles one query type.
    The ``raw`` argument is the ``data`` dict from Dgraph's JSON response.

    Date handling: Dgraph returns dates as ISO 8601 strings (e.g.
    ``"2012-11-01T00:00:00Z"``). Parsers convert these to milliseconds-since-
    epoch via ``iso8601_to_ms()`` or ``parse_dgraph_datetime()`` to match the
    LDBC integer format.

    Field-name divergences from LDBC schema (Dgraph key → schema field):
        IC3:  friendId → person_id, friendFirstName → person_first_name,
              friendLastName → person_last_name, xCount → x_count,
              yCount → y_count
        IC5:  forumName → forum_title
        IC9:  commentOrPostId → message_id, personId → person_id,
              content/imageFile → message_content,
              commentOrPostCreationDate → message_creation_date
        IC11: companyName → organization_name,
              workAt|workFrom → organization_work_from,
              personId → person_id
        IC14: personIds → person_ids_in_path
    """

    def parse(self, query_type: str, query_num: int, raw: Any) -> list:
        return self._dispatch(query_type, query_num, raw)

    # ── IC queries ──────────────────────────────────────────────────────

    def _parse_ic1(self, data: dict) -> list[IC1Result]:
        print("IC1: Performing client-side aggregation and sorting (Dgraph limitation)", file=sys.stderr)

        # Dgraph's @normalize flattens studyAt/workAt into scalar fields,
        # producing multiple rows per friend (one per university/company).
        # We aggregate across rows: collect unique universities and companies
        # per friendId, keeping the first row's scalar fields.
        aggregated: OrderedDict[int, dict] = OrderedDict()

        for distance, key in [(1, "friends1"), (2, "friends2"), (3, "friends3")]:
            if key not in data:
                continue

            for friend in data[key]:
                friend_id = parse_int_safe(friend.get("friendId"))

                if friend_id not in aggregated:
                    birthday_str = friend.get("friendBirthday", "")
                    creation_str = friend.get("friendCreationDate", "")
                    aggregated[friend_id] = {
                        "friend_id": friend_id,
                        "friend_last_name": friend.get("friendLastName", ""),
                        "distance": distance,
                        "friend_birthday": iso8601_to_ms(birthday_str) if birthday_str else 0,
                        "friend_creation_date": iso8601_to_ms(creation_str) if creation_str else 0,
                        "friend_gender": friend.get("friendGender", ""),
                        "friend_browser_used": friend.get("friendBrowserUsed", ""),
                        "friend_location_ip": friend.get("friendLocationIp", ""),
                        "friend_emails": sorted(normalize_list(friend.get("friendEmails", []))),
                        "friend_languages": sorted(normalize_list(friend.get("friendLanguages", []))),
                        "friend_city_name": friend.get("friendCityName", ""),
                        "universities": set(),
                        "companies": set(),
                    }

                acc = aggregated[friend_id]

                # Collect university from this row (flattened by @normalize)
                uni_name = friend.get("uniName")
                if uni_name:
                    acc["universities"].add((
                        uni_name,
                        parse_int_safe(friend.get("studyAt|classYear")),
                        friend.get("uniCityName", ""),
                    ))

                # Collect company from this row (flattened by @normalize)
                company_name = friend.get("companyName")
                if company_name:
                    acc["companies"].add((
                        company_name,
                        parse_int_safe(friend.get("workAt|workFrom")),
                        friend.get("companyCountryName", ""),
                    ))

        results = []
        for acc in aggregated.values():
            results.append(IC1Result(
                friend_id=acc["friend_id"],
                friend_last_name=acc["friend_last_name"],
                distance_from_person=acc["distance"],
                friend_birthday=acc["friend_birthday"],
                friend_creation_date=acc["friend_creation_date"],
                friend_gender=acc["friend_gender"],
                friend_browser_used=acc["friend_browser_used"],
                friend_location_ip=acc["friend_location_ip"],
                friend_emails=acc["friend_emails"],
                friend_languages=acc["friend_languages"],
                friend_city_name=acc["friend_city_name"],
                friend_universities=sorted(Organization(*t) for t in acc["universities"]),
                friend_companies=sorted(Organization(*t) for t in acc["companies"]),
            ))

        results.sort()
        return results[:RESULT_LIMITS["ic1"]]

    def _parse_ic2(self, data: dict) -> list[IC2Result]:
        results: list[IC2Result] = []

        for key in ("posts", "comments"):
            for row in data.get(key, []):
                results.append(IC2Result(
                    person_id=row["friendId"],
                    person_first_name=row["friendFirstName"],
                    person_last_name=row["friendLastName"],
                    message_id=row["messageId"],
                    message_content=row.get("messageContent", "") or row.get("messageImageFile", ""),
                    message_creation_date=parse_dgraph_datetime(row["messageCreationDate"]),
                ))

        return results

    def _parse_ic3(self, data: dict) -> list[IC3Result]:
        results = [
            IC3Result(
                person_id=row["friendId"],
                person_first_name=row["friendFirstName"],
                person_last_name=row["friendLastName"],
                x_count=row["xCount"],
                y_count=row["yCount"],
                count=row["count"],
            )
            for row in data.get("friends", [])
        ]
        results.sort(key=lambda x: (-x.count, x.person_id))
        return results[:RESULT_LIMITS["ic3"]]

    def _parse_ic4(self, data: dict) -> list[IC4Result]:
        results = [
            IC4Result(
                tag_name=tag.get("tagName", ""),
                post_count=parse_int_safe(tag.get("postCount")),
            )
            for tag in data.get("tags", [])
        ]
        results.sort(key=lambda x: (-x.post_count, x.tag_name))
        return results[:RESULT_LIMITS["ic4"]]

    def _parse_ic5(self, data: dict) -> list[IC5Result]:
        results = [
            IC5Result(
                forum_title=f.get("forumName", ""),
                post_count=parse_int_safe(f.get("postCount")),
                forum_id=parse_int_safe(f.get("forumId")),
            )
            for f in data.get("forums", [])
        ]
        results.sort(key=lambda x: (-x.post_count, x.forum_id))
        return results[:RESULT_LIMITS["ic5"]]

    def _parse_ic6(self, data: dict) -> list[IC6Result]:
        results = [
            IC6Result(
                tag_name=tag.get("tagName", ""),
                post_count=parse_int_safe(tag.get("postCount")),
            )
            for tag in data.get("tags", [])
        ]
        results.sort(key=lambda x: (-x.post_count, x.tag_name))
        return results[:RESULT_LIMITS["ic6"]]

    def _parse_ic7(self, data: dict) -> list[IC7Result]:
        results = [
            IC7Result(
                person_id=parse_int_safe(l.get("personId")),
                person_first_name=l.get("personFirstName", ""),
                person_last_name=l.get("personLastName", ""),
                like_creation_date=parse_int_safe(l.get("likeCreationDate")),
                message_id=parse_int_safe(l.get("messageId")),
                message_content=l.get("messageContent", ""),
                minutes_latency=parse_int_safe(l.get("minutesLatency")),
                is_new=bool(l.get("isNew", False)),
            )
            for l in data.get("likers", [])
        ]
        if "likers" not in data and "result" in data:
            return self._parse_ic7_result_block(data["result"])    # sorted + cut to 20 there
        results.sort()
        return results

    def _parse_ic7_result_block(self, rows: list) -> list[IC7Result]:
        """The repo's Dgraph IC7 text (every copy uses this shape; none uses `likers`):
        `result(func: uid(L), first: 200)` with each
        liker's newest like nested under `likes` (like date as a facet). The engine returns up to
        200 likers unordered; the LDBC answer is the client's top 20 by (likeCreationDate desc,
        personId asc)."""
        out = []
        for r in rows:
            likes = r.get("likes") or []
            if not likes:
                continue
            m = likes[0]
            like_ms = iso8601_to_ms(m.get("likesCreationDate"))
            msg_ms = iso8601_to_ms(m.get("messageCreationDate"))
            out.append(IC7Result(
                person_id=parse_int_safe(r.get("friendId")),
                person_first_name=r.get("friendFirstName", ""),
                person_last_name=r.get("friendLastName", ""),
                like_creation_date=like_ms,
                message_id=parse_int_safe(m.get("messageId")),
                message_content=m.get("messageContent") or m.get("messageImageFile", ""),
                minutes_latency=(like_ms - msg_ms) // 60000,
                is_new=bool(r.get("isNew", False)),
            ))
        out.sort(key=lambda x: (-x.like_creation_date, x.person_id))
        return out[:RESULT_LIMITS["ic7"]]

    def _parse_ic8(self, data: dict) -> list[IC8Result]:
        return [
            IC8Result(
                person_id=parse_int_safe(c.get("personId")),
                person_first_name=c.get("personFirstName", ""),
                person_last_name=c.get("personLastName", ""),
                comment_creation_date=parse_dgraph_datetime(c.get("commentCreationDate")),
                comment_id=parse_int_safe(c.get("commentId")),
                comment_content=c.get("commentContent", ""),
            )
            for c in data.get("recent", [])
        ]

    def _parse_ic9(self, data: dict) -> list[IC9Result]:
        return [
            IC9Result(
                person_id=parse_int_safe(msg.get("personId")),
                person_first_name=msg.get("personFirstName", ""),
                person_last_name=msg.get("personLastName", ""),
                message_id=parse_int_safe(msg.get("commentOrPostId")),
                message_content=msg.get("content", "") or msg.get("imageFile", ""),
                message_creation_date=parse_dgraph_datetime(msg.get("commentOrPostCreationDate", "")),
            )
            for msg in data.get("recent", [])
        ]

    def _parse_ic10(self, data: dict) -> list[IC10Result]:
        results = [
            IC10Result(
                person_id=parse_int_safe(p.get("personId")),
                person_first_name=p.get("personFirstName", ""),
                person_last_name=p.get("personLastName", ""),
                common_interest_score=parse_int_safe(p.get("commonInterestScore")),
                person_gender=p.get("personGender", ""),
                person_city_name=p.get("personCityName", ""),
            )
            for p in data.get("recs", [])
        ]
        results.sort()
        return results[:RESULT_LIMITS["ic10"]]

    def _parse_ic11(self, data: dict) -> list[IC11Result]:
        # An IC11 row is a (person, company, workFrom) triple. The query filters each person's own workAt
        # edges by workFrom, so a friend who qualified only through a company someone ELSE joined in time
        # comes back with no company: not an answer row.
        results = [
            IC11Result(
                person_id=parse_int_safe(p.get("personId")),
                person_first_name=p.get("personFirstName", ""),
                person_last_name=p.get("personLastName", ""),
                organization_name=p.get("companyName", ""),
                organization_work_from=parse_int_safe(p.get("workAt|workFrom")),
            )
            for p in data.get("results", [])
            if "companyName" in p and p.get("workAt|workFrom") is not None
        ]
        results.sort()
        return results[:RESULT_LIMITS["ic11"]]

    def _parse_ic12(self, data: dict) -> list[IC12Result]:
        print("IC12: Performing client-side aggregation (Dgraph limitation)", file=sys.stderr)

        aggregated: dict[int, dict] = defaultdict(lambda: {
            "person_id": 0,
            "person_first_name": "",
            "person_last_name": "",
            "tag_names": set(),
            "reply_count": 0,
        })

        for friend in data.get("friends", []):
            person_id = parse_int_safe(friend.get("friendId"))
            if person_id == 0:
                continue

            entry = aggregated[person_id]
            if entry["person_id"] == 0:
                entry["person_id"] = person_id
                entry["person_first_name"] = friend.get("friendFirstName", "")
                entry["person_last_name"] = friend.get("friendLastName", "")
                entry["reply_count"] = parse_int_safe(friend.get("replyCount"))

            tag_name = friend.get("tagNames", "")
            if tag_name:
                entry["tag_names"].add(tag_name)

        results = [
            IC12Result(
                person_id=parse_int_safe(d["person_id"]),
                person_first_name=str(d["person_first_name"]),
                person_last_name=str(d["person_last_name"]),
                tag_names=sorted(d["tag_names"]),
                reply_count=parse_int_safe(d["reply_count"]),
            )
            for d in aggregated.values()
        ]
        results.sort()
        return results

    def _parse_ic13(self, data: dict) -> IC13Result:
        return IC13Result(
            shortest_path_length=parse_int_safe(data.get("shortestPathLength", -1), default=-1),
        )

    def _parse_ic14(self, data: dict) -> list[IC14Result]:
        results = [
            IC14Result(
                person_ids_in_path=[parse_int_safe(pid) for pid in path.get("personIds", [])],
                path_weight=parse_float_safe(path.get("pathWeight")),
            )
            for path in data.get("paths", [])
        ]
        results.sort()
        return results

    # ── IS queries ──────────────────────────────────────────────────────

    def _parse_is1(self, data: dict) -> IS1Result:
        person = data.get("person", [{}])[0]
        birthday_str = person.get("birthday", "")
        creation_str = person.get("creationDate", "")
        return IS1Result(
            first_name=person.get("firstName", ""),
            last_name=person.get("lastName", ""),
            birthday=iso8601_to_ms(birthday_str) if birthday_str else 0,
            location_ip=person.get("locationIP", ""),
            browser_used=person.get("browserUsed", ""),
            city_id=parse_int_safe(person.get("cityId")),
            gender=person.get("gender", ""),
            creation_date=iso8601_to_ms(creation_str) if creation_str else 0,
        )

    def _parse_is2(self, data: dict) -> list[IS2Result]:
        if "posts" not in data and "comments" not in data and "person" in data:
            return self._parse_is2_person_block(data["person"])
        results: list[IS2Result] = []

        # Posts
        for msg in data.get("posts", []):
            message_id = parse_int_safe(msg.get("messageId"))
            creation_val = msg.get("messageCreationDate")
            creation_ms = (
                parse_dgraph_datetime(creation_val)
                if isinstance(creation_val, str)
                else parse_int_safe(creation_val)
            )
            content = msg.get("messageContent", "") or msg.get("messageImageFile", "")

            orig_post_id = parse_int_safe(msg.get("originalPostId"))
            orig_poster = msg.get("originalPoster", [])
            if isinstance(orig_poster, list) and orig_poster:
                op = orig_poster[0]
                orig_author_id = parse_int_safe(op.get("authorId"))
                orig_author_first = op.get("authorFirstName", "")
                orig_author_last = op.get("authorLastName", "")
            else:
                orig_author_id = 0
                orig_author_first = ""
                orig_author_last = ""

            results.append(IS2Result(
                message_id=message_id,
                message_content=content,
                message_creation_date=creation_ms,
                original_post_id=orig_post_id,
                original_post_author_id=orig_author_id,
                original_post_author_first_name=orig_author_first,
                original_post_author_last_name=orig_author_last,
            ))

        # Comments — walk replyOf chain to find the original post
        for msg in data.get("comments", []):
            message_id = parse_int_safe(msg.get("messageId"))
            creation_val = msg.get("messageCreationDate")
            creation_ms = (
                parse_dgraph_datetime(creation_val)
                if isinstance(creation_val, str)
                else parse_int_safe(creation_val)
            )
            content = msg.get("messageContent", "")

            orig_post_id = 0
            orig_author_id = 0
            orig_author_first = ""
            orig_author_last = ""

            current = msg.get("replyOf")
            for _ in range(3):
                if not current:
                    break
                if isinstance(current, list):
                    current = current[0] if current else None
                if not current:
                    break

                if "post" in current.get("postType", []):
                    orig_post_id = parse_int_safe(current.get("postId"))
                    authors = current.get("postAuthor", [])
                    if isinstance(authors, list) and authors:
                        author = authors[0]
                        orig_author_id = parse_int_safe(author.get("authorId"))
                        orig_author_first = author.get("authorFirstName", "")
                        orig_author_last = author.get("authorLastName", "")
                    break

                current = current.get("replyOf")

            results.append(IS2Result(
                message_id=message_id,
                message_content=content,
                message_creation_date=creation_ms,
                original_post_id=orig_post_id,
                original_post_author_id=orig_author_id,
                original_post_author_first_name=orig_author_first,
                original_post_author_last_name=orig_author_last,
            ))

        return results

    def _parse_is2_person_block(self, persons: list) -> list[IS2Result]:
        """The repo's Dgraph IS2 text (every copy uses this shape): one `person` block whose
        `messages` (already
        ordered and cut to 10 by the engine) carry the original post up to 3 replyOf levels:
        a Post is its own original (`selfAsOriginal` is its author); a Comment's original is
        `originalPost`, else `parentComment.grandParentPost`, else
        `parentComment.grandParentComment.greatGrandParentPost`."""
        def one(v):
            return (v[0] if v else None) if isinstance(v, list) else v

        results: list[IS2Result] = []
        for msg in (one(persons) or {}).get("messages", []):
            creation_val = msg.get("messageCreationDate")
            post_id, author = 0, None
            if "post" in (msg.get("messageType") or []):
                post_id, author = parse_int_safe(msg.get("messageId")), one(msg.get("selfAsOriginal"))
            else:
                parent = one(msg.get("parentComment")) or {}
                grand = one(parent.get("grandParentComment")) or {}
                orig = (one(msg.get("originalPost")) or one(parent.get("grandParentPost"))
                        or one(grand.get("greatGrandParentPost")))
                if orig:
                    post_id, author = parse_int_safe(orig.get("postId")), one(orig.get("originalPoster"))
            author = author or {}
            results.append(IS2Result(
                message_id=parse_int_safe(msg.get("messageId")),
                message_content=msg.get("content") or msg.get("imageFile", ""),
                message_creation_date=(parse_dgraph_datetime(creation_val)
                                       if isinstance(creation_val, str)
                                       else parse_int_safe(creation_val)),
                original_post_id=post_id,
                original_post_author_id=parse_int_safe(author.get("authorId")),
                original_post_author_first_name=author.get("authorFirstName", ""),
                original_post_author_last_name=author.get("authorLastName", ""),
            ))
        return results

    def _parse_is3(self, data: dict) -> list[IS3Result]:
        results: list[IS3Result] = []
        seen: set[int] = set()

        for person in data.get("person", []):
            for edge_key, date_key in [
                ("knows", "knows|creationDate"),
                ("~knows", "~knows|creationDate"),
            ]:
                for friend in person.get(edge_key, []):
                    friend_id = parse_int_safe(friend.get("friendId"))
                    if friend_id in seen:
                        continue
                    seen.add(friend_id)

                    creation_str = friend.get(date_key, "")
                    creation_ms = iso8601_to_ms(creation_str) if creation_str else 0

                    results.append(IS3Result(
                        person_id=friend_id,
                        first_name=friend.get("friendFirstName", ""),
                        last_name=friend.get("friendLastName", ""),
                        friendship_creation_date=creation_ms,
                    ))

        results.sort(key=lambda x: (-x.friendship_creation_date, x.person_id))
        return results

    def _parse_is4(self, data: dict) -> IS4Result:
        message = data.get("message", [{}])[0]
        creation_str = message.get("creationDate")
        creation_ms = iso8601_to_ms(creation_str) if creation_str else 0
        content = message.get("content")
        if not content:
            content = message.get("imageFile", "")
        return IS4Result(message_creation_date=creation_ms, message_content=content)

    def _parse_is5(self, data: dict) -> IS5Result:
        message = data.get("message", [{}])[0]
        creator = message.get("hasCreator", [{}])[0]
        return IS5Result(
            person_id=parse_int_safe(creator.get("authorId")),
            first_name=creator.get("authorFirstName", ""),
            last_name=creator.get("authorLastName", ""),
        )

    def _parse_is6(self, data: dict) -> IS6Result:
        forum_data = data.get("m", [{}])
        if not forum_data:
            return IS6Result(
                forum_id=0, forum_title="",
                moderator_id=0, moderator_first_name="", moderator_last_name="",
            )
        fd = forum_data[0]
        return IS6Result(
            forum_id=parse_int_safe(fd.get("forumId")),
            forum_title=fd.get("forumTitle", ""),
            moderator_id=parse_int_safe(fd.get("moderatorId")),
            moderator_first_name=fd.get("moderatorFirstName", ""),
            moderator_last_name=fd.get("moderatorLastName", ""),
        )

    def _parse_is7(self, data: dict) -> list[IS7Result]:
        results: list[IS7Result] = []

        for reply in data.get("replies", []):
            knows = bool(reply.get("knowsCount", 0))
            if not knows:
                knows = bool(reply.get("knowsCount2", 0))

            results.append(IS7Result(
                comment_id=parse_int_safe(reply.get("commentId")),
                comment_content=reply.get("commentContent", ""),
                comment_creation_date=parse_dgraph_datetime(reply.get("commentCreationDate")),
                reply_author_id=parse_int_safe(reply.get("replyAuthorId")),
                reply_author_first_name=reply.get("replyAuthorFirstName", ""),
                reply_author_last_name=reply.get("replyAuthorLastName", ""),
                reply_author_knows_original_message_author=knows,
            ))

        return results
