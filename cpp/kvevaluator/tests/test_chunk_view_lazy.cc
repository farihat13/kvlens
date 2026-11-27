// The lists::ChunkView over the skip16 header (ENDs, the edge column, skip samples and labels read in place, ordinals and
// the props view built on first use): every accessor equals an independent decode of the same data (each owner's
// Condensed list, lists::View, and the encoder's inputs), on k = 1 to 4,682 owners (every skip-sample edge: 15, 16,
// 17, 31, 32, 33), label_bits 0 / 1 / 3, and the three chunk kinds (no props, inline props, ordinals); every absent
// id between two owners finds nothing; 64-bit fields (a chunk spanning two vid groups); a constant-degree edge column;
// a corrupt sample or gap stream caught by validate(). Decode counts: the constructor books the chunk once, a lazy build books nothing more. The trailing-bytes
// check fires at construction (no props or ordinals) or at validate(). Eight threads' concurrent first touch of one
// shared view read what one thread reads (a race shows only under TSan, which ctest does not build). Fresh views probed
// out of order (present and absent ids, each accessor first), and eight threads' finds on one fresh view, give the
// in-order answers.
#include <atomic>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "codec/decode_counts.h"
#include "codec/lists.h"
#include "codec/varint.h"
#include "formats/rows.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;

static const std::vector<ColDecl> kNone = {};
static const std::vector<ColDecl> kEdge = {{"creationDate", "date", false}, {"note", "str", false}};

enum class Kind { Plain, Props, Ords };
static const char* name(Kind k) { return k == Kind::Plain ? "plain" : k == Kind::Props ? "props" : "ords"; }

struct Case {
  std::vector<lists::ChunkOwner> owners;
  std::vector<Value> rows;          // one per edge, owner order (Props)
  std::vector<uint64_t> ords;       // one per edge, owner order (Ords)
  std::string raw;
};

static uint64_t lcg(uint64_t& s) { s = s * 6364136223846793005ULL + 1442695040888963407ULL; return s >> 33; }

static Case make(size_t k, unsigned bits, Kind kind, uint64_t seed) {
  Case c;
  uint64_t s = seed, src = 1000;
  for (size_t o = 0; o < k; o++) {
    src += 1 + lcg(s) % 300;
    lists::ChunkOwner ow{src, {}, {}};
    size_t deg = (o % 5 == 3) ? 0 : 1 + lcg(s) % (o % 7 == 0 ? 40 : 4);   // some owners empty, some wide
    uint64_t d = lcg(s) % 50;
    for (size_t j = 0; j < deg; j++) {
      d += 1 + lcg(s) % (j % 3 == 0 ? 100000 : 9);
      ow.dsts.push_back(d);
      if (bits) ow.labels.push_back(uint8_t(lcg(s) % (1u << bits)));
    }
    c.owners.push_back(ow);
  }
  size_t e = 0;
  for (auto& o : c.owners)
    for (size_t j = 0; j < o.dsts.size(); j++, e++) {
      if (kind == Kind::Props)
        c.rows.push_back(e % 4 == 2 ? Value::row({{"creationDate", Value(int64_t(e))}})
                                    : Value::row({{"creationDate", Value(int64_t(e))}, {"note", Value(std::string(e % 13, 'n'))}}));
      if (kind == Kind::Ords) c.ords.push_back(lcg(s) % 2000000);
    }
  std::vector<PropRow> pr;
  for (size_t i = 0; i < c.rows.size(); i++) pr.push_back({i, &c.rows[i]});
  ValueRows vr(kEdge, pr);
  c.raw = lists::encode_chunk(c.owners, bits, kind == Kind::Props ? kEdge : kNone, kind == Kind::Props ? &vr : nullptr,
                              kind == Kind::Ords ? &c.ords : nullptr);
  return c;
}

static const std::vector<ColDecl>* cols_of(Kind kind) { return kind == Kind::Props ? &kEdge : &kNone; }

// every accessor of `cv` against the owner's Condensed list and the encoder's inputs; returns the entries compared
static size_t compare(const lists::ChunkView& cv, const Case& c, unsigned bits, Kind kind, const std::string& tag) {
  size_t compared = 0, base = 0;
  CHECK(cv.size() == c.owners.size(), tag << ": " << cv.size() << " owners");
  for (size_t i = 0; i < c.owners.size() && i < cv.size(); i++) {
    const auto& o = c.owners[i];
    CHECK(cv.src(i) == o.src && cv.find(o.src) == int64_t(i), tag << " owner " << i << ": src / find");
    if (i + 1 < c.owners.size() && c.owners[i + 1].src > o.src + 1)
      CHECK(cv.find(o.src + 1) == -1 && cv.find(c.owners[i + 1].src - 1) == -1, tag << " owner " << i << ": absent ids after it");
    std::vector<PropRow> mine;
    for (size_t j = 0; j < o.dsts.size() && kind == Kind::Props; j++) mine.push_back({j, &c.rows[base + j]});
    ValueRows mr(kEdge, mine);
    std::string list = lists::encode(o.dsts, bits ? &o.labels : nullptr, bits, *cols_of(kind), kind == Kind::Props ? &mr : nullptr);
    lists::View lv(cols_of(kind), list, bits);
    std::vector<uint64_t> d = cv.dsts(i);
    std::vector<uint8_t> w = cv.id_widths(i);
    CHECK(d == o.dsts && d == lv.dsts(), tag << " owner " << i << ": dsts");
    CHECK(w.size() == lv.size(), tag << " owner " << i << ": widths");
    if (bits || kind != Kind::Plain) CHECK(cv.edge_base(i) == base, tag << " owner " << i << ": edge_base");
    for (size_t j = 0; j < o.dsts.size() && j < lv.size() && j < w.size(); j++, compared++) {
      CHECK(cv.label(i, j) == lv.label(j) && cv.label(i, j) == (bits ? o.labels[j] : 0), tag << " owner " << i << " entry " << j << ": label");
      CHECK(w[j] == lv.id_bytes(j), tag << " owner " << i << " entry " << j << ": id width");
      CHECK(cv.props_bytes(i, j) == lv.props_bytes(j), tag << " owner " << i << " entry " << j << ": props bytes");
      CHECK(to_json(cv.props_row(i, j)) == to_json(lv.props_row(j)), tag << " owner " << i << " entry " << j << ": props row");
      if (kind == Kind::Ords) CHECK(cv.ordinal(i, j) == c.ords[base + j], tag << " owner " << i << " entry " << j << ": ordinal");
    }
    base += o.dsts.size();
  }
  CHECK((cv.props() != nullptr) == (kind == Kind::Props), tag << ": props view present iff props");
  return compared;
}

static void check_shapes() {
  size_t cases = 0, entries = 0;
  for (size_t k : {1, 2, 7, 15, 16, 17, 31, 32, 33, 1000, 4682})
    for (unsigned bits : {0u, 1u, 3u})
      for (Kind kind : {Kind::Plain, Kind::Props, Kind::Ords}) {
        Case c = make(k, bits, kind, 17 + k * 31 + bits);
        std::string tag = std::string(name(kind)) + " k=" + std::to_string(k) + " bits=" + std::to_string(bits);
        DecodeCounts b = decode_counts();
        lists::ChunkView cv(cols_of(kind), c.raw, bits, kind == Kind::Ords);
        DecodeCounts ctor = decode_counts().since(b);
        DecodeCounts want;
        want.chunks = 1;
        want.bytes = c.raw.size();
        CHECK(ctor == want, tag << ": the constructor books the chunk once (bytes " << ctor.bytes << " of " << c.raw.size() << ")");
        b = decode_counts();
        cv.validate();                                         // the first lazy builds
        cv.validate();
        CHECK(decode_counts().since(b) == DecodeCounts(), tag << ": a lazy build books nothing");
        entries += compare(cv, c, bits, kind, tag);
        cases++;
      }
  CHECK(cases == 99, "cases " << cases);
  std::cerr << "  shapes: " << cases << " chunks, " << entries << " entries compared\n";
}

static bool throws(const std::function<void()>& f) {
  try { f(); } catch (const std::exception&) { return true; }
  return false;
}

static void check_trailing_bytes() {
  for (Kind kind : {Kind::Plain, Kind::Props, Kind::Ords}) {
    Case c = make(7, 1, kind, 5);
    std::string bad = c.raw + '\0';
    if (kind == Kind::Plain) {
      CHECK(throws([&] { lists::ChunkView v(cols_of(kind), bad, 1, false); }), "plain: trailing byte refused at construction");
    } else {
      lists::ChunkView v(cols_of(kind), bad, 1, kind == Kind::Ords);   // lazy: the check waits for the first build
      CHECK(throws([&] { v.validate(); }), name(kind) << ": trailing byte refused at validate()");
      CHECK(throws([&] { v.validate(); }), name(kind) << ": and again (a failed build is not marked done)");
    }
    lists::ChunkView good(cols_of(kind), c.raw, 1, kind == Kind::Ords);
    CHECK(!throws([&] { good.validate(); }), name(kind) << ": the good chunk validates");
  }
  Case c = make(7, 3, Kind::Plain, 9);
  CHECK(throws([&] { lists::ChunkView v(&kNone, c.raw.substr(0, c.raw.size() - 1), 3, false); }), "a truncated label block is refused");
  CHECK(throws([&] { lists::ChunkView v(&kEdge, c.raw, 3, true); }), "ordinals beside columns are refused");
}

static void check_concurrent_first_touch() {
  for (Kind kind : {Kind::Props, Kind::Ords}) {
    Case c = make(1000, 3, kind, 77);
    auto cv = std::make_shared<const lists::ChunkView>(cols_of(kind), c.raw, 3, kind == Kind::Ords);
    std::atomic<bool> go{false};
    std::vector<std::string> seen(8);
    std::vector<std::thread> ts;
    for (size_t t = 0; t < 8; t++)
      ts.emplace_back([&, t] {
        while (!go.load()) std::this_thread::yield();
        std::string out;
        for (size_t i = 0; i < cv->size(); i++)
          for (size_t j = 0; j < c.owners[i].dsts.size(); j++)
            out += kind == Kind::Ords ? std::to_string(cv->ordinal(i, j)) + "," : to_json(cv->props_row(i, j)).dump() + ",";
        seen[t] = out;
      });
    go.store(true);
    for (auto& t : ts) t.join();
    std::string one;
    {
      lists::ChunkView solo(cols_of(kind), c.raw, 3, kind == Kind::Ords);
      for (size_t i = 0; i < solo.size(); i++)
        for (size_t j = 0; j < c.owners[i].dsts.size(); j++)
          one += kind == Kind::Ords ? std::to_string(solo.ordinal(i, j)) + "," : to_json(solo.props_row(i, j)).dump() + ",";
    }
    for (size_t t = 0; t < 8; t++) CHECK(seen[t] == one && !one.empty(), name(kind) << ": thread " << t << " read what one thread reads");
  }
}

// a fresh view per probe order: find / src / dsts / edge_base / label first touched out of index order
static void check_out_of_order() {
  size_t probes = 0;
  for (unsigned bits : {0u, 3u})
    for (Kind kind : {Kind::Plain, Kind::Props, Kind::Ords}) {
      Case c = make(1000, bits, kind, 101 + bits);
      std::string tag = std::string(name(kind)) + " bits=" + std::to_string(bits);
      size_t k = c.owners.size();
      std::vector<size_t> base(k, 0);
      for (size_t i = 1; i < k; i++) base[i] = base[i - 1] + c.owners[i - 1].dsts.size();
      {
        lists::ChunkView cv(cols_of(kind), c.raw, bits, kind == Kind::Ords);
        CHECK(cv.first() == c.owners.front().src && cv.last() == c.owners.back().src, tag << ": first / last before any parse");
        CHECK(cv.find(c.owners.front().src - 1) == -1 && cv.find(c.owners.back().src + 1) == -1, tag << ": outside the chunk");
        CHECK(cv.find(c.owners[500].src + 1) == -1, tag << ": an absent id between owners, first probe");
        CHECK(cv.find(c.owners[500].src) == 500 && cv.find(c.owners[3].src) == 3, tag << ": then present ids behind it");
        CHECK(cv.find(c.owners.back().src) == int64_t(k - 1), tag << ": the last owner");
        probes += 6;
      }
      for (int order = 0; order < 3; order++) {               // reversed, a stride, random
        lists::ChunkView cv(cols_of(kind), c.raw, bits, kind == Kind::Ords);
        uint64_t s = 7 + order;
        for (size_t n = 0; n < k; n++) {
          size_t i = order == 0 ? k - 1 - n : order == 1 ? (n * 389) % k : size_t(lcg(s) % k);
          const auto& o = c.owners[i];
          bool between = (n % 4 == 1) && i + 1 < k && c.owners[i + 1].src > o.src + 1;
          if (between) { CHECK(cv.find(o.src + 1) == -1, tag << " order " << order << ": absent id after owner " << i); probes++; continue; }
          switch (n % 3) {                                     // which accessor touches the owner first
            case 0: CHECK(cv.find(o.src) == int64_t(i), tag << " order " << order << " owner " << i << ": find"); break;
            case 1: CHECK(cv.src(i) == o.src, tag << " order " << order << " owner " << i << ": src"); break;
            default: CHECK(cv.dsts(i) == o.dsts, tag << " order " << order << " owner " << i << ": dsts"); break;
          }
          bool per_edge = bits || kind != Kind::Plain;   // edge_base exists only where the family stores per edge
          CHECK(cv.dsts(i) == o.dsts && (!per_edge || cv.edge_base(i) == base[i]) && cv.src(i) == o.src,
                tag << " order " << order << " owner " << i << ": dsts / base / src");
          for (size_t j = 0; j < o.dsts.size(); j++) {
            if (bits) CHECK(cv.label(i, j) == o.labels[j], tag << " order " << order << " owner " << i << ": label " << j);
            if (kind == Kind::Ords) CHECK(cv.ordinal(i, j) == c.ords[base[i] + j], tag << " owner " << i << ": ordinal " << j);
            if (kind == Kind::Props)
              CHECK(to_json(cv.props_row(i, j)) == to_json(c.rows[base[i] + j]), tag << " owner " << i << ": props row " << j);
          }
          probes++;
        }
      }
    }
  lists::ChunkView empty(&kNone, lists::encode_chunk({}, 0, kNone, nullptr), 0);
  CHECK(empty.size() == 0 && empty.find(5) == -1, "an empty chunk finds nothing");
  CHECK(probes > 6 * 3 * 900, "probes " << probes);
  std::cerr << "  out of order: " << probes << " probes on fresh views\n";
}

// 64-bit fields: owners in two vid groups (the top nibble differs: a write can land one in another group's chunk), so
// the id span needs 64 bits and a sample's two fields 64 + w_pos; a wide dst area; and a constant-degree edge column
static void check_wide_and_constant() {
  std::vector<lists::ChunkOwner> owners;
  for (size_t o = 0; o < 40; o++) {
    uint64_t src = o < 20 ? 3 + o * 1000003 : (uint64_t(0xF) << 60) + o * 7;
    lists::ChunkOwner ow{src, {}, {}};
    for (size_t j = 0; j < 2; j++) { ow.dsts.push_back((uint64_t(0xE) << 60) + o * 10 + j); ow.labels.push_back(uint8_t(j)); }
    owners.push_back(ow);
  }
  std::string raw = lists::encode_chunk(owners, 1, kNone, nullptr);
  lists::ChunkView cv(&kNone, raw, 1, false);
  bool ok = cv.size() == 40 && cv.first() == owners.front().src && cv.last() == owners.back().src;
  for (size_t i = 0; i < 40; i++)
    ok = ok && cv.find(owners[i].src) == int64_t(i) && cv.src(i) == owners[i].src && cv.dsts(i) == owners[i].dsts &&
         cv.edge_base(i) == 2 * i && cv.label(i, 1) == 1 && cv.find(owners[i].src + 1) == -1;
  ok = ok && cv.owners().size() == 40 && cv.owners()[39] == owners[39].src;
  CHECK(ok, "two vid groups (64-bit span): every owner, its dsts, its edge base and its labels");
  CHECK(!throws([&] { cv.validate(); }), "two vid groups: validates");
  // one owner's degree changed: the edge column switches from mode 0 (2 B) to CUM fields, and reads back
  std::vector<lists::ChunkOwner> mixed = owners;
  mixed[5].dsts.push_back((uint64_t(0xE) << 60) + 59);
  mixed[5].labels.push_back(0);
  lists::ChunkView mv(&kNone, lists::encode_chunk(mixed, 1, kNone, nullptr), 1, false);
  bool mok = true;
  for (size_t i = 0; i < 40; i++)
    mok = mok && mv.dsts(i) == mixed[i].dsts && mv.edge_base(i) == 2 * i + (i > 5) && mv.label(i, 1) == 1;
  CHECK(mok && !throws([&] { mv.validate(); }), "mixed degrees: the CUM edge column reads back");
}

// validate() walks every field: a corrupt sample, gap stream or END is caught (each must throw), the good chunk is not
static void check_validate_catches() {
  Case c = make(100, 1, Kind::Ords, 3);
  lists::ChunkView good(&kNone, c.raw, 1, true);
  CHECK(!throws([&] { good.validate(); }), "the good chunk validates");
  // the header ends where the dst area starts: the bytes after it are the lists, the label block and the ordinals
  size_t tail = (c.ords.size() * 1 + 7) / 8;
  for (auto& o : c.owners) {
    std::string l = lists::encode(o.dsts, nullptr, 0, kNone, nullptr);
    size_t n = 0;
    lists::View lv(&kNone, l, 0);
    for (size_t j = 0; j < lv.size(); j++) n += lv.id_bytes(j);
    tail += n;
  }
  for (uint64_t x : c.ords) { std::string u; put_uvarint(u, x); tail += u.size(); }
  size_t header = c.raw.size() - tail, caught = 0, tried = 0;
  for (size_t at = 1; at < header; at++) {   // every header, sample and gap byte, its low bit flipped
    std::string bad = c.raw;
    bad[at] = char(uint8_t(bad[at]) ^ 0x01);
    tried++;
    try { lists::ChunkView v(&kNone, bad, 1, true); v.validate(); } catch (const std::exception&) { caught++; }
  }   // a flip in a padding bit or inside a dst id can validate: no checksum, as before the skip16 header
  CHECK(tried > 100 && caught * 10 >= tried * 9, "validate caught " << caught << " of " << tried << " one-bit flips in the header");
  std::cerr << "  validate: " << caught << " of " << tried << " one-bit flips caught\n";
}

// eight threads find owners in different orders on ONE fresh shared view
static void check_concurrent_find() {
  Case c = make(1000, 3, Kind::Plain, 55);
  size_t k = c.owners.size(), bad = 0;
  for (int round = 0; round < 20; round++) {
    auto cv = std::make_shared<const lists::ChunkView>(&kNone, c.raw, 3, false);
    std::atomic<bool> go{false};
    std::atomic<size_t> wrong{0};
    std::vector<std::thread> ts;
    for (size_t t = 0; t < 8; t++)
      ts.emplace_back([&, t] {
        while (!go.load()) std::this_thread::yield();
        uint64_t s = 1 + t * 1000 + size_t(round);
        for (size_t n = 0; n < 400; n++) {
          size_t i = t % 2 ? size_t(lcg(s) % k) : (t * 131 + n * 7) % k;
          if (cv->find(c.owners[i].src) != int64_t(i) || cv->dsts(i) != c.owners[i].dsts) wrong++;
        }
      });
    go.store(true);
    for (auto& t : ts) t.join();
    bad += wrong.load();
  }
  CHECK(bad == 0, "concurrent finds: " << bad << " wrong of " << 20 * 8 * 400);
}

TEST_CASE(test_chunk_view_lazy) {
  check_shapes();
  check_trailing_bytes();
  check_concurrent_first_touch();
  check_out_of_order();
  check_concurrent_find();
  check_wide_and_constant();
  check_validate_catches();
  std::cerr << "test_chunk_view_lazy: " << checks << " checks, " << fails << " failed\n";
  return fails ? 1 : 0;
}

}  // namespace
