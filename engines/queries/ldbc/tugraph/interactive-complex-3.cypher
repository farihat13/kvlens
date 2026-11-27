// LDBC Interactive Complex 3 on TuGraph (v4.5.2, its default Cypher front end): friends and friends of friends who
// posted in both of two countries in a period, and live in neither. Written from the shared Cypher form
// (engines/queries/ldbc/cypher/interactive-complex-3.cypher); the changes, each because TuGraph's Cypher does not
// accept the shared text:
//   1. The start person's variable is `person`, not `start`: START is a reserved word in TuGraph's grammar
//      ("no viable alternative at input 'MATCH (start'").
//   2. Each country's message count is count(DISTINCT CASE ... THEN m ELSE null END), not the size of a list
//      comprehension over collect(DISTINCT CASE ...): TuGraph refuses the comprehension's variable ("Variable `m`
//      not defined") and fails collect(DISTINCT) over a node ("Internal error: Step"). The two are equal in Cypher:
//      collect and count both skip nulls, so the comprehension's IS NOT NULL filter removes nothing.
//   3. The last column is named count without backticks (TuGraph keeps the backticks in the column's name).
// The same reads otherwise: two OPTIONAL MATCHes over each person's messages (a Post or a Comment: no shared
// label, so the message is unlabelled) and their country, then the person's own city.
// Params: $personId, $countryXName, $countryYName, $startDate, $endDate (epoch ms, startDate + durationDays).
MATCH (person:Person {id: $personId})-[:KNOWS*1..2]-(other:Person)
WHERE other.id <> $personId
WITH DISTINCT other
OPTIONAL MATCH (other)<-[:HAS_CREATOR]-(mx)-[:IS_LOCATED_IN]->(px:Place)
WITH other, count(DISTINCT CASE
       WHEN px.name = $countryXName
        AND mx.creationDate >= $startDate AND mx.creationDate < $endDate
       THEN mx ELSE null END) AS cntX
OPTIONAL MATCH (other)<-[:HAS_CREATOR]-(my)-[:IS_LOCATED_IN]->(py:Place)
WITH other, cntX, count(DISTINCT CASE
       WHEN py.name = $countryYName
        AND my.creationDate >= $startDate AND my.creationDate < $endDate
       THEN my ELSE null END) AS cntY
WHERE cntX > 0 AND cntY > 0
MATCH (other)-[:IS_LOCATED_IN]->(loc:Place)
WHERE loc.name <> $countryXName AND loc.name <> $countryYName
RETURN other.id AS personId, other.firstName AS firstName, other.lastName AS lastName,
       cntX AS messagesInX, cntY AS messagesInY, cntX + cntY AS count
ORDER BY count DESC, personId ASC
LIMIT 20
