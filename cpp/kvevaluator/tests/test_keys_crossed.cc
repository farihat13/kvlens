// keys_crossed (KvPairHists, store/size_hist.h): a cursor landing on a key, not declared a miss, whose value nobody
// read. Booked when the cursor leaves the landing, when it dies, and when its task COMMITS with the cursor still alive
// (the landing it rests on); never twice; a read value or a declared miss is not a crossing (the injections).
#include <cstdlib>
#include <iostream>
#include <string>
#include <unistd.h>

#include "store/counting.h"
#include "store/rocksdb_store.h"
#include "harness.h"

namespace {

using namespace evalcpp;


// what the store committed since the last call: (keys crossed, keys its range reads returned)
static uint64_t g_values_read = 0;   // of the last committed() call
static std::pair<uint64_t, uint64_t> committed(CountingStore& st) {
  HistParts h;
  st.take_committed_hists(h);
  uint64_t crossed = 0, keys = 0;
  g_values_read = 0;
  for (auto& p : h) { crossed += p.keys_crossed; keys += p.range_keys.sum; g_values_read += p.values_read; }
  return {crossed, keys};
}

TEST_CASE(test_keys_crossed) {
  char tmpl[] = "/tmp/evalcpp-crossed-XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  StoreOptions so;
  auto store = Store::open_read_write(dir + "/db", so, true);
  for (auto [k, v] : {std::pair{"aa1", "1"}, std::pair{"aa2", "22"}, std::pair{"cc1", "333"}, std::pair{"ee1", ""}})
    if (!store->db()->Put(rocksdb::WriteOptions(), k, v).ok()) return 2;
  CountingStore st(*store);
  st.parts_commit(); committed(st);

  {  // 1. three landings, no value read: three keys crossed, booked by the time the cursor is dead
    { Cursor c = st.seek("aa"); c.next(); c.next(); }
    st.parts_commit();
    auto [crossed, keys] = committed(st);
    CHECK_EQ(crossed, 3u); CHECK_EQ(keys, 3u);
  }
  {  // 2. one value read, one crossed, the last landing declared a miss (the end of a scan)
    { Cursor c = st.seek("aa"); c.value(); c.next(); c.next(); c.mark_miss(); }
    st.parts_commit();
    auto [crossed, keys] = committed(st);
    CHECK_EQ(crossed, 1u); CHECK_EQ(keys, 2u);
  }
  {  // 3. the cursor is ALIVE at its task's commit: the landing it rests on is booked by the commit, once
    Cursor c = st.seek("aa");
    c.next();                                   // aa1 crossed (left), resting on aa2 unread
    st.parts_commit();
    auto [crossed, keys] = committed(st);
    CHECK_EQ(crossed, 2u); CHECK_EQ(keys, 2u);
  }                                             // ... and its death adds nothing
  st.parts_commit();
  { auto [crossed, keys] = committed(st); CHECK_EQ(crossed, 0u); CHECK_EQ(keys, 0u); }
  {  // 4. injection: the same, but the resting landing's value IS read before the commit: one crossing fewer
    Cursor c = st.seek("aa");
    c.next();
    c.value();
    st.parts_commit();
    auto [crossed, keys] = committed(st);
    CHECK_EQ(crossed, 1u); CHECK_EQ(keys, 2u);
  }
  st.parts_commit(); committed(st);
  {  // 5. injection: the resting landing declared a miss before the commit
    Cursor c = st.seek("aa");
    c.next();
    c.mark_miss();
    st.parts_commit();
    auto [crossed, keys] = committed(st);
    CHECK_EQ(crossed, 1u); CHECK_EQ(keys, 1u);
  }
  st.parts_commit(); committed(st);
  {  // 6. a discard drops the open landing with its range read: nothing is booked, then or at the cursor's death
    { Cursor c = st.seek("aa"); st.parts_discard(); }
    st.parts_commit();
    auto [crossed, keys] = committed(st);
    CHECK_EQ(crossed, 0u); CHECK_EQ(keys, 0u);
  }
  st.parts_commit(); committed(st);
  {  // 7. a value that holds no byte: reading it reads nothing, the key stays crossed (a Split record with no code and
     //    no props); and every returned key is crossed or read: keys == crossed + values read
    { Cursor c = st.seek("cc"); c.value(); c.next(); c.value(); c.next(); c.mark_miss(); }   // cc1 (3 B, read), ee1 (empty)
    st.parts_commit();
    auto [crossed, keys] = committed(st);
    CHECK_EQ(crossed, 1u); CHECK_EQ(keys, 2u); CHECK_EQ(g_values_read, 1u);
  }
  std::cout << "keys crossed: " << checks << " checks, " << fails << " failed\n";
  return fails ? 1 : 0;
}

}  // namespace
