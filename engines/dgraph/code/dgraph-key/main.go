package main

import (
	"bufio"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"

	"github.com/pkg/errors"

	// The tool recognizes various prefix bytes (DefaultPrefix, ByteSchema, ByteType, ByteSplit, ByteUnused)
	// "github.com/hypermodeinc/dgraph/v25/codec"
	"github.com/hypermodeinc/dgraph/v25/codec"
	"github.com/hypermodeinc/dgraph/v25/protos/pb"
	"github.com/hypermodeinc/dgraph/v25/types"
	"github.com/hypermodeinc/dgraph/v25/x"
	"google.golang.org/protobuf/proto"
)

const (
	// keys.go
	// [Prefix Byte]
	DefaultPrefix = byte(0x00)
	ByteSchema    = byte(0x01)
	ByteType      = byte(0x02)
	ByteSplit     = byte(0x04)
	ByteUnused    = byte(0xff)

	// keys.go
	// [Type Byte]
	ByteData     = byte(0x00)
	ByteIndex    = byte(0x02)
	ByteReverse  = byte(0x04)
	ByteCount    = byte(0x08)
	ByteCountRev = ByteCount | ByteReverse

	Set uint32 = 0x01
	Del uint32 = 0x02
	Ovr uint32 = 0x03

	// userMeta bits
	BitSchemaPosting   byte = 0x01
	BitDeltaPosting    byte = 0x04
	BitCompletePosting byte = 0x08
	BitEmptyPosting    byte = 0x10
)

// Lookup tables
var bytePrefixNames = map[byte]string{
	DefaultPrefix: "ByteData",
	ByteSchema:    "ByteSchema",
	ByteType:      "ByteType",
	ByteSplit:     "ByteSplit",
	ByteUnused:    "ByteUnused",
}

var byteTypeNames = map[byte]string{
	ByteData:     "ByteData",
	ByteIndex:    "ByteIndex",
	ByteReverse:  "ByteReverse",
	ByteCount:    "ByteCount",
	ByteCountRev: "ByteCountRev",
}

var userMetaNames = map[byte]string{
	BitSchemaPosting:   "BitSchemaPosting",
	BitDeltaPosting:    "BitDeltaPosting",
	BitCompletePosting: "BitCompletePosting",
	BitEmptyPosting:    "BitEmptyPosting",
}

var opNames = map[uint32]string{
	Set: "SET",
	Del: "DEL",
	Ovr: "OVR",
}

// Inspired by x/key.go:Parse
// ParseKeyJSON parses a hexadecimal-encoded Dgraph key and returns both a structured
// ParsedKey object and a map containing human-readable key components.
//
// General key format:
//   - If ByteSchema or ByteType: [PrefixByte(1byte)][Namespace(8bytes)][AttrLen(2bytes)][Attr(varlen)]
//   - Else:                      [PrefixByte(1byte)][Namespace(8bytes)][AttrLen(2bytes)][Attr(varlen)][TypeByte(1byte)][Type-specific Data]
//
// [Type-specific Data] varies based on TypeByte
// Details:
//   - For ByteData or ByteReverse:   [UID (8 bytes)][StartUID (8 bytes, optional)]
//   - For ByteIndex:                 [Term (var len)][StartUID (8 bytes, optional)]
//   - For ByteCount or ByteCountRev: [Count (4 bytes)][StartUID (8 bytes, optional)]
//
// Parameters:
//   - hexKey: A hexadecimal string representation of the Dgraph key to parse
//
// Returns:
//   - x.ParsedKey: A structured representation of the parsed key components
//   - map[string]interface{}: A map containing human-readable key information including:
//   - "BytePrefix": Name of the prefix byte or "Unknown" if unrecognized
//   - "Namespace": Namespace ID as uint64
//   - "AttrSize": Size of the attribute field
//   - "Attr": Attribute name as string
//   - "TypeByte": Name of the type byte or "Unknown" if unrecognized
//   - "HasStartUid": Boolean indicating if StartUID is present
//   - Additional fields based on type: "UID", "StartUID", "Term", "Count"
//   - error: Error if key parsing fails
func ParseKeyJSON(hexKey string) (x.ParsedKey, map[string]interface{}, error) {
	result := make(map[string]interface{})
	key, err := hex.DecodeString(hexKey)
	if err != nil {
		return x.ParsedKey{}, result, fmt.Errorf("failed to decode hex key: %v", err)
	}

	var p x.ParsedKey

	if len(key) < 9 {
		return p, result, errors.New("Key length less than 9")
	}

	// -----------------------------------------------
	// Extract:
	// [PrefixByte(1byte)][Namespace(8bytes)][AttrLen(2bytes)][Attr(varlen)]
	// -----------------------------------------------

	bytePrefix := key[0]
	namespace := key[1:9]
	key = key[9:]

	// Identify [Prefix Byte] and write to result
	if name, exists := bytePrefixNames[bytePrefix]; exists {
		result["BytePrefix"] = name
	} else {
		result["BytePrefix"] = fmt.Sprintf("Unknown (%d)", bytePrefix)
	}
	result["Namespace"] = binary.BigEndian.Uint64(namespace)

	// Handle unused keys
	if bytePrefix == ByteUnused {
		result["KeyStatus"] = "Key to be discarded"
		return p, result, nil
	}

	// Check if ByteSplit is set
	p.HasStartUid = (bytePrefix == ByteSplit)
	if p.HasStartUid {
		result["HasStartUid"] = true
	} else {
		result["HasStartUid"] = false
	}

	// [attr size (2 bytes)][attr (var len)]... must be at least 3 bytes
	if len(key) < 3 {
		return p, result, errors.Errorf("Invalid format for key %v", key)
	}

	// Extract attribute size and attribute
	sz := int(binary.BigEndian.Uint16(key[:2]))
	k := key[2:]
	result["AttrSize"] = sz
	if len(k) < sz {
		return p, result, errors.Errorf("Invalid size %v for key %v", sz, key)
	}
	// p.Attr = x.NamespaceAttr(binary.BigEndian.Uint64(namespace), string(k[:sz]))
	result["Attr"] = string(k[:sz])

	// If ByteSchema or ByteType, we're done
	switch bytePrefix {
	case ByteSchema, ByteType:
		return p, result, nil
	default:
	}

	// -----------------------------------------------
	// Extract:
	// ...[TypeByte(1byte)][Type-specific Data]
	// -----------------------------------------------

	// Move past attribute
	k = k[sz:]

	// Extract [Type Byte] and move forward
	p.ByteType = k[0]
	k = k[1:]

	// Identify [Type Byte] and write to result
	if name, exists := byteTypeNames[p.ByteType]; exists {
		result["TypeByte"] = name
	} else {
		result["TypeByte"] = fmt.Sprintf("Unknown (%d)", p.ByteType)
	}

	// Parse type-specific data
	// Handle based on Type Byte
	// Details:
	//  - For ByteData or ByteReverse:   [UID (8 bytes)][StartUID (8 bytes, optional)]
	//  - For ByteIndex:                 [Term (var len)][StartUID (8 bytes, optional)]
	//  - For ByteCount or ByteCountRev: [Count (4 bytes)][StartUID (8 bytes, optional)]
	// Note:
	// - StartUID is present if HasStartUid is true
	// - UID and Count are big-endian encoded
	// - Term is UTF-8 string
	// - For ByteData/ByteReverse, UID must be present and non-zero
	// - For ByteCount/ByteCountRev, Count must be present
	// - For ByteIndex, Term must be present (can be empty string), StartUID is optional
	// - If StartUID is present, it must be the last 8 bytes of the key
	switch p.ByteType {
	case ByteData, ByteReverse:
		if len(k) < 8 {
			return p, result, errors.Errorf("uid length < 8 for key: %q, parsed key: %+v", key, p)
		}
		p.Uid = binary.BigEndian.Uint64(k)
		result["UID"] = p.Uid
		if p.Uid == 0 {
			return p, result, errors.Errorf("Invalid UID with value 0 for key: %v", key)
		}
		if p.HasStartUid {
			if len(k) != 16 {
				return p, result, errors.Errorf("StartUid length != 8 for key: %q, parsed key: %+v", key, p)
			}
			k = k[8:]
			p.StartUid = binary.BigEndian.Uint64(k)
			result["StartUID"] = p.StartUid
		}
	case ByteIndex:
		if !p.HasStartUid {
			p.Term = string(k)
			result["Term"] = p.Term
			break
		}
		// else if p.HasStartUid
		if len(k) < 8 {
			return p, result, errors.Errorf("StartUid length < 8 for key: %q, parsed key: %+v", key, p)
		}
		term := k[:len(k)-8]
		startUid := k[len(k)-8:]
		p.Term = string(term)
		p.StartUid = binary.BigEndian.Uint64(startUid)
		result["Term"] = p.Term
		result["StartUID"] = p.StartUid
	case ByteCount, ByteCountRev:
		if len(k) < 4 {
			return p, result, errors.Errorf("count length < 4 for key: %q, parsed key: %+v", key, p)
		}
		p.Count = binary.BigEndian.Uint32(k)
		result["Count"] = p.Count
		if !p.HasStartUid {
			break
		}
		if p.HasStartUid {
			if len(k) != 12 {
				return p, result, errors.Errorf("StartUid length != 8 for key: %q, parsed key: %+v", key, p)
			}
			k = k[4:]
			p.StartUid = binary.BigEndian.Uint64(k)
			result["StartUID"] = p.StartUid
		}
	default:
		return p, result, errors.Errorf("Invalid data type")
	}
	return p, result, nil
}

func ParseValueJSON(hexValue string) (map[string]interface{}, error) {
	result := make(map[string]interface{})
	valueBytes, err := hex.DecodeString(hexValue)
	if err != nil {
		return result, err
	}
	typeAttempts := []types.TypeID{
		types.StringID, types.IntID, types.FloatID,
		types.BoolID, types.DateTimeID,
	}
	for _, typeID := range typeAttempts {
		if val, err := types.Convert(
			types.Val{Tid: types.BinaryID, Value: valueBytes},
			typeID,
		); err == nil {
			result[typeID.Name()] = val.Value
		}
	}
	detectedType, parsedValue := types.TypeForValue(valueBytes)
	result["auto_detected_type"] = detectedType.Name()
	result["auto_detected_value"] = parsedValue
	return result, nil
}

// ParseKeyValuePair parses a Dgraph key-value pair from hexadecimal strings and returns
// structured information about both the key and value components.
//
// The function decodes the provided hex strings and interprets the value based on the
// userMeta byte, which indicates the type of data stored (empty posting, complete posting,
// delta posting, schema posting, or raw value).
//
// Parameters:
//   - keyHex: Hexadecimal string representation of the key
//   - valueHex: Hexadecimal string representation of the value
//   - version: Version number associated with the key-value pair
//   - userMeta: Byte indicating the type of value stored (BitEmptyPosting, BitCompletePosting, etc.)
//   - schema: Map of attribute names to their expected TypeID for schema-based parsing
//
// Returns:
//   - map[string]interface{}: Structured representation containing:
//   - "key": Parsed key information
//   - "posting_list": Parsed PostingList got from proto.Unmarshal (if applicable)
//   - "value": Parsed value (format depends on userMeta)
//   - "version": Version number (if non-zero)
//   - "userMeta": Human-readable name of the userMeta type
//   - Additional fields specific to the value type (posting_list, schema_type, etc.)
//   - error: Any error encountered during parsing
//
// The function handles different value types:
//   - BitEmptyPosting: Returns nil value
//   - BitCompletePosting/BitDeltaPosting: Unmarshals as PostingList protobuf
//   - BitSchemaPosting: Unmarshals as SchemaUpdate or TypeUpdate based on key type
//   - Default: Attempts schema-based parsing or auto-detection for raw values
func ParseKeyValuePair(keyHex, valueHex string, version uint64, userMeta byte, schema map[string]types.TypeID) (map[string]interface{}, error) {
	result := make(map[string]interface{})

	// Parse key
	_, keyResult, err := ParseKeyJSON(keyHex)
	// Add key information
	result["key"] = keyResult
	if err != nil {
		return result, err
	}

	// Parse value based on userMeta (like ReadPostingList does)
	valueBytes, err := hex.DecodeString(valueHex)
	if err != nil {
		return result, err
	}

	// Add metadata
	if name, exists := userMetaNames[userMeta]; exists {
		result["metadata"] = map[string]interface{}{
			"userMeta": name,
			"version":  version,
		}
	} else {
		result["metadata"] = map[string]interface{}{
			"userMeta": fmt.Sprintf("Unknown (%d)", userMeta),
			"version":  version,
		}
	}

	if version != 0 {
		result["metadata"].(map[string]interface{})["version"] = version
	}

	switch userMeta {
	case BitEmptyPosting: // 0x01
		result["value"] = nil

	case BitCompletePosting: // 0x02
		plist := &pb.PostingList{}
		// Parse the complete posting using protobuf
		if err := proto.Unmarshal(valueBytes, plist); err != nil {
			return result, fmt.Errorf("failed to unmarshal complete posting: %v", err)
		}
		result["posting_list"] = plist

		// Parse the posting list
		parsedPostings, err := parsePostingList(plist, keyResult["Attr"].(string), schema)
		if err != nil {
			return result, err
		}
		result["value"] = parsedPostings

	case BitDeltaPosting: // 0x04
		plist := &pb.PostingList{}
		if err := proto.Unmarshal(valueBytes, plist); err != nil {
			return result, fmt.Errorf("failed to unmarshal delta posting: %v", err)
		}
		result["posting_list"] = plist

		// Parse the posting list
		parsedPostings, err := parsePostingList(plist, keyResult["Attr"].(string), schema)
		if err != nil {
			return result, err
		}
		result["value"] = parsedPostings

	case BitSchemaPosting: // 0x08
		// Parse key to determine if it's schema or type
		keyBytes, _ := hex.DecodeString(keyHex)
		parsedKey, err := x.Parse(keyBytes)
		if err != nil {
			return result, fmt.Errorf("failed to parse key: %v", err)
		}

		if parsedKey.IsSchema() {
			// This is a schema update
			var update pb.SchemaUpdate
			if err := proto.Unmarshal(valueBytes, &update); err != nil {
				return result, fmt.Errorf("failed to unmarshal schema update: %v", err)
			}
			index_specs_strs := []string{}
			for _, spec := range update.IndexSpecs {
				index_specs_strs = append(index_specs_strs, spec.String())
			}
			result["value"] = map[string]interface{}{
				"predicate":        update.Predicate,
				"valuetype":        update.ValueType.String(),
				"directive":        update.Directive.String(),
				"tokenizer":        update.Tokenizer,
				"count":            update.Count,
				"list":             update.List,
				"upsert":           update.Upsert,
				"lang":             update.Lang,
				"unique":           update.Unique,
				"object_type_name": update.ObjectTypeName,
				"no_conflict":      update.NoConflict,
				"index_specs":      index_specs_strs,
			}
			result["schema_type"] = "SchemaUpdate"

		} else if parsedKey.IsType() {
			// This is a type update
			var update pb.TypeUpdate
			if err := proto.Unmarshal(valueBytes, &update); err != nil {
				return result, fmt.Errorf("failed to unmarshal type update: %v", err)
			}

			var fields []map[string]interface{}
			for _, field := range update.Fields {
				index_specs_strs := []string{}
				for _, spec := range field.IndexSpecs {
					index_specs_strs = append(index_specs_strs, spec.String())
				}
				fields = append(fields, map[string]interface{}{
					"predicate":        field.Predicate,
					"valuetype":        field.ValueType.String(),
					"directive":        field.Directive.String(),
					"tokenizer":        field.Tokenizer,
					"count":            field.Count,
					"list":             field.List,
					"upsert":           field.Upsert,
					"lang":             field.Lang,
					"unique":           field.Unique,
					"object_type_name": field.ObjectTypeName,
					"no_conflict":      field.NoConflict,
					"index_specs":      index_specs_strs,
				})
			}

			result["value"] = map[string]interface{}{
				"type_name": update.TypeName,
				"fields":    fields,
			}
			result["schema_type"] = "TypeUpdate"

		} else {
			return result, fmt.Errorf("unknown schema key type")
		}

	default:
		// Try to parse as raw value with schema
		attr := keyResult["Attr"].(string)
		if expectedType, exists := schema[attr]; exists {
			val, err := types.Convert(
				types.Val{Tid: types.BinaryID, Value: valueBytes},
				expectedType,
			)
			if err == nil {
				result["value"] = val.Value
				result["value_type"] = expectedType.Name()
				result["parsing_method"] = "schema_based_raw"
			} else {
				result["value"] = fmt.Sprintf("raw_bytes: %x", valueBytes)
				result["parsing_method"] = "raw_bytes"
				result["error"] = err.Error()
			}
		} else {
			// Auto-detect
			detectedType, parsedValue := types.TypeForValue(valueBytes)
			result["value"] = parsedValue
			result["value_type"] = detectedType.Name()
			result["parsing_method"] = "auto_detected_raw"
		}
	}

	return result, nil
}

// --- Posting List Parsing ---
// protos/pb/pb.pb.go
// The PostingList format includes:
//   - Pack: An optional packed list of UIDs for efficient storage of large UID sets.
//   - Postings: An array of Posting objects, each with fields:
//   - CommitTs: The commit timestamp for the posting.
//   - Splits: An optional array of split information for sharded posting lists.
//
// The Posting format includes:
//   - Uid: The UID this posting refers to (for edges) or is associated with (for values).
//   - Value: Raw bytes for the value (if PostingType is value).
//   - ValType: Enum for the value type (string, int, float, etc).
//   - PostingType: Enum indicating the type (value, UID, etc).
//   - LangTag: Optional language tag for multi-lingual predicates.
//   - Facets: Optional metadata key-value pairs attached to the posting.
//   - Op: Operation type (SET=1, DEL=2, OVR=3).
//   - StartTs: The start timestamp for the posting (for delta postings).
//   - CommitTs: The commit timestamp for the posting list.
//
// parsePostingList converts a protobuf PostingList into a map representation for easier inspection and debugging.
// It extracts and formats all posting information including UIDs, operation types, values, facets, and metadata.
//
// Parameters:
//   - plist: The protobuf PostingList to parse
//   - attr: The attribute name associated with this posting list
//   - schema: A map of attribute names to their type IDs for proper value parsing
//
// Returns:
//   - map[string]interface{}: A structured representation containing:
//   - "postings": Array of posting objects with uid, operation type, value type, operation, language tag, value, and facets
//   - "commit_ts": The commit timestamp of the posting list
//   - "pack": Optional packed UID information with count and list of UIDs
//   - "splits": Optional split information if the posting list is split across multiple parts
//   - error: Any error encountered during parsing, particularly from value parsing
//
// The function handles different operation types (SET, DEL, OVR), parses values according to their types,
// processes facets, and extracts packed UID information when present. If value parsing fails, both the
// error and raw hex representation are included in the result.
func parsePostingList(plist *pb.PostingList, attr string, schema map[string]types.TypeID) (map[string]interface{}, error) {
	result := make(map[string]interface{})

	var parsedPostings []map[string]interface{}
	for _, posting := range plist.Postings {
		parsedPosting := make(map[string]interface{})
		parsedPosting["uid"] = posting.Uid
		parsedPosting["posting_type"] = posting.PostingType.String()
		parsedPosting["val_type"] = posting.ValType.String()
		if name, exists := opNames[posting.Op]; exists {
			parsedPosting["op"] = name
		} else {
			parsedPosting["op"] = fmt.Sprintf("Unknown (%d)", posting.Op)
		}

		if len(posting.LangTag) > 0 {
			parsedPosting["lang_tag"] = string(posting.LangTag)
		}

		// if schema length is zero, set attribute type to string
		if len(schema) == 0 {
			schema[attr] = types.StringID
		}

		// Parse the value based on ValType
		if len(posting.Value) > 0 {
			parsedValue, err := parsePostingValue(posting.Value, posting.ValType, attr, schema)
			if err != nil {
				parsedPosting["value_error"] = err.Error()
				parsedPosting["value_raw"] = fmt.Sprintf("%x", posting.Value)
			} else {
				parsedPosting["value"] = parsedValue
			}
		}

		// Parse facets if present
		if len(posting.Facets) > 0 {
			facets := make(map[string]interface{})
			for _, facet := range posting.Facets {
				facetValue, _ := parsePostingValue(facet.Value, pb.Posting_ValType(facet.ValType), "", schema)
				facets[facet.Key] = facetValue
			}
			parsedPosting["facets"] = facets
		}

		parsedPostings = append(parsedPostings, parsedPosting)
	}

	result["postings"] = parsedPostings
	result["commit_ts"] = plist.CommitTs

	// Parse pack (compressed UID list) if present
	if plist.Pack != nil {
		dec := &codec.Decoder{Pack: plist.Pack}
		uids := dec.Seek(0, codec.SeekStart)
		result["pack"] = map[string]interface{}{
			"uid_count": len(uids),
			"uids":      uids, // These are the actual UIDs
		}
	}

	// Parse splits if present
	if len(plist.Splits) > 0 {
		result["splits"] = plist.Splits
	}

	return result, nil
}

// parsePostingValue converts raw byte data to the appropriate Go type based on the specified value type.
// It handles various Dgraph posting value types including strings, integers, floats, booleans,
// datetimes, and UIDs. For unrecognized types, it attempts to use the provided schema to convert
// the value, falling back to string representation if conversion fails.
//
// Parameters:
//   - valueBytes: Raw byte data to be parsed
//   - valType: The expected posting value type from protobuf definition
//   - attr: Attribute name used for schema lookup in fallback cases
//   - schema: Map of attribute names to their expected type IDs for type conversion
//
// Returns:
//   - interface{}: Parsed value in the appropriate Go type, or nil for UID types
//   - error: Error if parsing fails due to invalid data format or length
//
// Supported types:
//   - STRING: Returns string representation
//   - INT: Returns uint64 (requires 8-byte input)
//   - FLOAT: Returns float64 (requires 8-byte input)
//   - BOOL: Returns bool (true if first byte is 0x01)
//   - DATETIME: Returns string representation
//   - UID: Returns nil
//   - Default: Attempts schema-based conversion, falls back to string
func parsePostingValue(valueBytes []byte, valType pb.Posting_ValType, attr string, schema map[string]types.TypeID) (interface{}, error) {
	switch valType {
	case pb.Posting_STRING:
		return string(valueBytes), nil
	case pb.Posting_INT:
		if len(valueBytes) == 8 {
			return int64(binary.LittleEndian.Uint64(valueBytes)), nil
		}
		return nil, fmt.Errorf("invalid int length: %d", len(valueBytes))
	case pb.Posting_FLOAT:
		if len(valueBytes) == 8 {
			bits := binary.LittleEndian.Uint64(valueBytes)
			return math.Float64frombits(bits), nil
		}
		return nil, fmt.Errorf("invalid float length: %d", len(valueBytes))
	case pb.Posting_BOOL:
		return len(valueBytes) > 0 && valueBytes[0] == 0x01, nil
	case pb.Posting_DATETIME:
		return string(valueBytes), nil // RFC3339 format
	case pb.Posting_UID:
		// For UID postings, the value is typically empty and the UID is in the Uid field
		return nil, nil
	default:
		// Try schema-based parsing as fallback
		if expectedType, exists := schema[attr]; exists {
			val, err := types.Convert(
				types.Val{Tid: types.BinaryID, Value: valueBytes},
				expectedType,
			)
			if err == nil {
				return val.Value, nil
			}
		}
		// Return as string if all else fails
		return string(valueBytes), nil
	}
}

// --------------------------------
// Main program and CLI handling
// --------------------------------

func TypeIDByName() map[string]types.TypeID {
	return map[string]types.TypeID{
		"string":   types.StringID,
		"int":      types.IntID,
		"float":    types.FloatID,
		"bool":     types.BoolID,
		"datetime": types.DateTimeID,
		"geo":      types.GeoID,
		"uid":      types.UidID,
		"password": types.PasswordID,
		"binary":   types.BinaryID,
		// "object":   types.ObjectID,
		"bigfloat": types.BigFloatID,
		"vfloat":   types.VFloatID,
	}
}

func parseSchemaString(schemaStr string) (map[string]types.TypeID, error) {
	schema := make(map[string]types.TypeID)
	if schemaStr == "" {
		return schema, nil
	}
	pairs := strings.Split(schemaStr, ",")
	for _, pair := range pairs {
		parts := strings.Split(pair, ":")
		if len(parts) != 2 {
			return nil, fmt.Errorf("invalid schema format: %s", pair)
		}
		attr := parts[0]
		typeStr := parts[1]
		typeID, ok := TypeIDByName()[typeStr]
		if !ok {
			return nil, fmt.Errorf("unknown type in schema: %s", typeStr)
		}
		schema[attr] = typeID
	}
	return schema, nil
}

// runBatch reads tab-separated lines from stdin and writes one JSON line per
// entry to stdout. Input format: key\tvalue\tversion\tuserMeta
// This avoids the overhead of spawning one process per key.
func runBatch(schema map[string]types.TypeID) {
	scanner := bufio.NewScanner(os.Stdin)
	// Allow up to 10 MB per line (large posting list values)
	scanner.Buffer(make([]byte, 0, 64*1024), 10*1024*1024)
	writer := bufio.NewWriter(os.Stdout)
	defer writer.Flush()

	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			continue
		}
		fields := strings.Split(line, "\t")
		if len(fields) < 4 {
			errJSON, _ := json.Marshal(map[string]interface{}{"error": "expected 4 tab-separated fields"})
			writer.Write(errJSON)
			writer.WriteByte('\n')
			writer.Flush()
			continue
		}
		keyHex := fields[0]
		valHex := fields[1]
		version, _ := strconv.ParseUint(fields[2], 10, 64)
		um, _ := strconv.ParseUint(fields[3], 10, 8)

		result, err := ParseKeyValuePair(keyHex, valHex, version, byte(um), schema)
		if err != nil {
			result["error"] = err.Error()
		}
		jsonBytes, _ := json.Marshal(result)
		writer.Write(jsonBytes)
		writer.WriteByte('\n')
		writer.Flush()
	}
}

func usage() {
	fmt.Fprintf(os.Stderr, `Usage:
  go run main.go [options]

Options:
`)
	fmt.Fprintf(os.Stderr, `
Operations:
  --op=key --key=<hex> [--schema=attr:type,...]
	Parse and display information about the given hex-encoded key.
  --op=val --key=<hex> --value=<hex> [--schema=attr:type,...]
	Parse and display information about the given hex-encoded value.
  --op=kv --key=<hex> --value=<hex> [--version=<num>] [--userMeta=<num>] [--schema=attr:type,...]
	Parse and display information about the given hex-encoded key-value pair,
	using version and userMeta as metadata for the value.
  --op=batch [--schema=attr:type,...]
	Batch mode: read tab-separated lines (key\tvalue\tversion\tuserMeta) from
	stdin, output one JSON line per entry to stdout.
  --op=help
 	Display this help message.
Schema Format:
  The schema should be provided in the format attr:type,attr:type,...
  Supported types: string, int, float, bool, datetime, geo, uid, password, binary, bigfloat, vfloat
Examples:
  go run main.go --op=key --key=010000000000000000000b6467726170682e74797065
  go run main.go --op=val --key=... --value=0a19088002121408ffffffffffffffffff01120500000000001801121a09ffffffffffffffff1205416c69636518092001600168047005
  go run main.go --op=kv --key=... --value=... --version=6 --userMeta=8 --schema=name:string,age:int
`)
	os.Exit(1)
}

func main() {
	flag.Usage = usage

	op := flag.String("op", "key", "Operation: key, val, kv, fastkey, help")
	keyHex := flag.String("key", "", "Hex-encoded key")
	valHex := flag.String("value", "", "Hex-encoded value (required for val/kv)")
	userMeta := flag.Uint("userMeta", 0, "userMeta byte (required for val/kv)")
	version := flag.Uint64("version", 0, "Version (optional)")
	schemaStr := flag.String("schema", "", "Schema in attr:type,... format (optional)")
	flag.Parse()
	schema, err := parseSchemaString(*schemaStr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to parse schema: %v\n", err)
		os.Exit(1)
	}
	// Batch mode doesn't need --key
	if *op == "batch" {
		runBatch(schema)
		return
	}

	// Common check
	if *keyHex == "" {
		result := map[string]interface{}{
			"error": "--key is required",
		}
		jsonBytes, _ := json.MarshalIndent(result, "", "  ")
		fmt.Fprintln(os.Stderr, string(jsonBytes))
		// fmt.Fprintln(os.Stderr, "Error: --key is required")
		os.Exit(1)
	}

	switch *op {
	case "help":
		usage()
	case "key":
		_, result, err := ParseKeyJSON(*keyHex)
		if err != nil {
			result["error"] = fmt.Sprintf("Failed to parse key: %v", err)
			jsonBytes, _ := json.MarshalIndent(result, "", "  ")
			fmt.Fprintln(os.Stderr, string(jsonBytes))
			os.Exit(1)
		}
		jsonBytes, _ := json.MarshalIndent(result, "", "  ")
		fmt.Println(string(jsonBytes))
	case "val":
		if *valHex == "" {
			fmt.Fprintln(os.Stderr, "Error: --value is required for --op=val")
			os.Exit(1)
		}
		result, err := ParseValueJSON(*valHex)
		if err != nil {
			result["error"] = fmt.Sprintf("Failed to parse value: %v", err)
			jsonBytes, _ := json.MarshalIndent(result, "", "  ")
			fmt.Fprintln(os.Stderr, string(jsonBytes))
			os.Exit(1)
		}
		jsonBytes, _ := json.MarshalIndent(result, "", "  ")
		fmt.Println(string(jsonBytes))
	case "kv":
		if *valHex == "" {
			result := map[string]interface{}{
				"error": "--value is required for --op=kv",
			}
			jsonBytes, _ := json.MarshalIndent(result, "", "  ")
			fmt.Fprintln(os.Stderr, string(jsonBytes))
			os.Exit(1)
		}
		result, err := ParseKeyValuePair(*keyHex, *valHex, *version, byte(*userMeta), schema)
		if err != nil {
			result["error"] = fmt.Sprintf("Failed to parse key-value pair: %v", err)
			jsonBytes, _ := json.MarshalIndent(result, "", "  ")
			fmt.Fprintln(os.Stderr, string(jsonBytes))
			os.Exit(1)
		}
		jsonBytes, _ := json.MarshalIndent(result, "", "  ")
		fmt.Println(string(jsonBytes))
	default:
		fmt.Fprintf(os.Stderr, "Unknown op: %s\n", *op)
		os.Exit(1)
	}
}
