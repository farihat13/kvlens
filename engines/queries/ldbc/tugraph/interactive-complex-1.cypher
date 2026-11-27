// LDBC Interactive Complex 1 on TuGraph (v4.5.2, its default Cypher front end): friends within 3 hops with a given
// first name. Written from the shared Cypher form (engines/queries/ldbc/cypher/interactive-complex-1.cypher); the
// changes, each because TuGraph's Cypher does not accept the shared text:
//   1. The first name is a WHERE filter, not a property map on the far end of the variable-length pattern: a
//      parameter there fails ("visit(...) failed", ast_expr_evaluator.cpp:480). The plan is the same either way
//      (EXPLAIN: an index seek on the start person, the variable-length expand, then a filter on firstName).
//   2. A WITH between each MATCH and the OPTIONAL MATCH after it: one query part takes one MATCH clause
//      ("Function not implemented yet: AddClause", src/cypher/parser/clause.h 300).
//   3. Each university and company is one string, '|name|year|place|', not a list: TuGraph's REST answer renders a
//      list as one unquoted string ("[a,b]"), and 480 organisation names at SF0.3 hold a comma, so a list of lists
//      cannot be read back. The `|` cannot occur in a name (LDBC's files are |-separated); live_answers.py reads it.
//   4. The columns are named as NebulaGraph's query names them (friendId, ..., friendCompanies), so its answer
//      parser reads TuGraph's answer. emails and languages are one string each, joined by ';' (TuGraph has no list
//      property): live_answers.py splits them.
// The same reads otherwise: the friend's city, then its universities with their city, then its companies with
// their country. Params: $personId, $firstName.
MATCH p = (person:Person {id: $personId})-[:KNOWS*1..3]-(friend:Person)
WHERE friend.firstName = $firstName AND friend.id <> $personId
WITH friend, min(length(p)) AS distance
MATCH (friend)-[:IS_LOCATED_IN]->(friendCity:Place)
WITH friend, friendCity, distance
OPTIONAL MATCH (friend)-[studyAt:STUDY_AT]->(uni:Organisation)-[:IS_LOCATED_IN]->(uniCity:Place)
WITH friend, friendCity, distance,
     collect(CASE uni.name WHEN null THEN null
             ELSE '|' + uni.name + '|' + toString(studyAt.classYear) + '|' + uniCity.name + '|' END) AS unis
OPTIONAL MATCH (friend)-[workAt:WORK_AT]->(company:Organisation)-[:IS_LOCATED_IN]->(companyCountry:Place)
WITH friend, friendCity, distance, unis,
     collect(CASE company.name WHEN null THEN null
             ELSE '|' + company.name + '|' + toString(workAt.workFrom) + '|' + companyCountry.name + '|' END) AS companies
RETURN friend.id AS friendId, friend.lastName AS friendLastName,
       distance AS distanceFromPerson, friend.birthday AS friendBirthday,
       friend.creationDate AS friendCreationDate, friend.gender AS friendGender,
       friend.browserUsed AS friendBrowserUsed, friend.locationIP AS friendLocationIp,
       friend.emails AS friendEmails, friend.languages AS friendLanguages, friendCity.name AS friendCityName,
       unis AS friendUniversities, companies AS friendCompanies
ORDER BY distanceFromPerson ASC, friendLastName ASC, friendId ASC
LIMIT 20
