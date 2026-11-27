// microbench hop1p7_knows on TuGraph (v4.5.2, its default Cypher front end): a person's distinct KNOWS neighbours
// (both directions: TuGraph stores KNOWS once per pair, as LDBC gives it), seven properties each, sorted by firstName.
// The shared Cypher form (engines/queries/microbench/cypher/hop1p7_knows.cypher) as written: TuGraph runs it unchanged.
// Param: $personId (LDBC's id, TuGraph's INT64 primary key `id`).
MATCH (p:Person {id: $personId})-[:KNOWS]-(f:Person)
WITH DISTINCT f
RETURN f.firstName, f.lastName, f.gender, f.birthday,
       f.creationDate, f.locationIP, f.browserUsed
ORDER BY f.firstName
