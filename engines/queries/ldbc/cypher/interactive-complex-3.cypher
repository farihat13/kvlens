// Cypher form, equivalent to the engines' LDBC Interactive Complex 3 queries (engines/queries/ldbc/<engine>/): friends
// and friends of friends who posted in both of two countries in a period. Standard Cypher written from NebulaGraph's
// openCypher text. Params: $personId, $countryXName, $countryYName, $startDate, $endDate.
MATCH (start:Person {id: $personId})-[:KNOWS*1..2]-(other:Person)
WHERE other.id <> $personId
WITH DISTINCT other
OPTIONAL MATCH (other)<-[:HAS_CREATOR]-(mx)-[:IS_LOCATED_IN]->(px:Place)
WITH other, collect(DISTINCT CASE
       WHEN px.name = $countryXName
        AND mx.creationDate >= $startDate AND mx.creationDate < $endDate
       THEN mx ELSE null END) AS mxList
WITH other, size([m IN mxList WHERE m IS NOT NULL]) AS cntX
OPTIONAL MATCH (other)<-[:HAS_CREATOR]-(my)-[:IS_LOCATED_IN]->(py:Place)
WITH other, cntX, collect(DISTINCT CASE
       WHEN py.name = $countryYName
        AND my.creationDate >= $startDate AND my.creationDate < $endDate
       THEN my ELSE null END) AS myList
WITH other, cntX, size([m IN myList WHERE m IS NOT NULL]) AS cntY
WHERE cntX > 0 AND cntY > 0
MATCH (other)-[:IS_LOCATED_IN]->(loc:Place)
WHERE loc.name <> $countryXName AND loc.name <> $countryYName
RETURN other.id AS personId, other.firstName, other.lastName,
       cntX AS messagesInX, cntY AS messagesInY, cntX + cntY AS `count`
ORDER BY `count` DESC, personId ASC
LIMIT 20
