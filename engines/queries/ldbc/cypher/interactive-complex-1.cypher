// Cypher form, equivalent to the engines' LDBC Interactive Complex 1 queries (engines/queries/ldbc/<engine>/): friends
// within 3 hops with a given first name. Standard Cypher written from NebulaGraph's openCypher text.
// Params: $personId, $firstName.
MATCH p = (person:Person {id: $personId})-[:KNOWS*1..3]-(friend:Person {firstName: $firstName})
WHERE friend.id <> $personId
WITH friend, min(length(p)) AS distance
MATCH (friend)-[:IS_LOCATED_IN]->(friendCity:Place)
OPTIONAL MATCH (friend)-[studyAt:STUDY_AT]->(uni:Organisation)-[:IS_LOCATED_IN]->(uniCity:Place)
WITH friend, friendCity, distance,
     collect(CASE uni.name WHEN null THEN null
             ELSE [uni.name, studyAt.classYear, uniCity.name] END) AS unis
OPTIONAL MATCH (friend)-[workAt:WORK_AT]->(company:Organisation)-[:IS_LOCATED_IN]->(companyCountry:Place)
WITH friend, friendCity, distance, unis,
     collect(CASE company.name WHEN null THEN null
             ELSE [company.name, workAt.workFrom, companyCountry.name] END) AS companies
RETURN friend.id AS friendId, friend.lastName AS friendLastName,
       distance AS distanceFromPerson, friend.birthday, friend.creationDate,
       friend.gender, friend.browserUsed, friend.locationIP,
       friend.emails, friend.languages, friendCity.name,
       unis, companies
ORDER BY distanceFromPerson ASC, friendLastName ASC, friendId ASC
LIMIT 20
