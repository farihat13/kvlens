// A pool built with pin_cpus pins its calling thread (worker 0: a server's request thread) for the request; when the
// pool ends, the caller's CPU mask is the one it had before, so a later request without pin_cpus is not left pinned.
#include <sched.h>
#include <unistd.h>

#include <filesystem>
#include <iostream>

#include "exec/pool.h"
#include "store/rocksdb_store.h"
#include "harness.h"

namespace {

using namespace evalcpp;

TEST_CASE(test_pool_pin) {
  cpu_set_t before;
  if (sched_getaffinity(0, sizeof before, &before) != 0) { std::cerr << "sched_getaffinity failed\n"; return 1; }
  int first = -1;
  for (int c = 0; c < CPU_SETSIZE && first < 0; c++)
    if (CPU_ISSET(c, &before)) first = c;
  if (CPU_COUNT(&before) < 2) { return kvt::skip("test_pool_pin: one usable CPU, pinning cannot change the mask"); }
  char tmpl[] = "/tmp/test_pool_pin_XXXXXX";
  std::string dir = kvt::temp_dir(tmpl);
  {
    StoreOptions so;
    auto store = Store::open_read_write(dir + "/db", so, true);
    {
      ThreadPool pool(*store, 1, {first}, 2, false, nullptr, false);
      cpu_set_t during;
      sched_getaffinity(0, sizeof during, &during);
      CHECK(CPU_COUNT(&during) == 1 && CPU_ISSET(first, &during), "the pool did not pin its caller");
    }
    cpu_set_t after;
    sched_getaffinity(0, sizeof after, &after);
    CHECK(CPU_EQUAL(&before, &after), "the caller is still pinned after the pool ended (" << CPU_COUNT(&after) << " of "
                                          << CPU_COUNT(&before) << " CPUs)");
  }
  std::filesystem::remove_all(dir);
  if (fails) return 1;
  std::cout << "test_pool_pin ok (" << CPU_COUNT(&before) << " CPUs restored)\n";
  return 0;
}

}  // namespace
