// Cypher form, equivalent to the engines' hop1p7_knows queries (engines/queries/microbench/<engine>/): a person's
// friends, seven properties each. Param: $personId.
MATCH (p:Person {id: $personId})-[:KNOWS]-(f:Person)
WITH DISTINCT f
RETURN f.firstName, f.lastName, f.gender, f.birthday,
       f.creationDate, f.locationIP, f.browserUsed
ORDER BY f.firstName
