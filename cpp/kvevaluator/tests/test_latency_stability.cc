// The latency stability test: a storage step must cost the same in a fresh process and after the process has run
// other work. The step is the evaluator's own SEEK (CountingStore::seek: a new RocksDB iterator per SEEK) on a 59 KB
// value, and the copy of that value into a string (what a chunked read does before decoding). "Other work" is a
// synthetic allocation history: 600 MB of mixed-size objects (median ~8 KB, a tail past 100 KB), a quarter kept
// alive between the freed ones, like the heap a few LDBC queries leave. On glibc 2.39 this gives a slowdown like the
// measured one (warm latency depends on what ran before in the process: here iterator + Seek
// 1.7 -> 11 us; in the SF30 profile, malloc alone 0.6 -> 8.6 us within that step), so this test FAILS on a glibc build
// and passes on tcmalloc (CMakeLists.txt EVALUATOR_ALLOCATOR). Bound: each step after the history within 10% of fresh.
// Fresh and after are two processes measured in turns, not one process before and after: the test runs itself again
// as the fresh process (--fresh), which does the same prelude (its own store, built the same way) and skips only the
// history; both are pinned to the same CPU and take alternate passes through a pipe (rounds FA, AF, FA, ...; a warm-up
// pass each first). A cell's ratio is the median over the rounds of after / fresh, so a load that starts or stops
// mid-test slows both sides, and a few bad rounds are outvoted. (measured one after the other, a load on
// the same CCD that started between the two windows failed the copy cell at 1.13-2.2x: the 59 KB copy reads an 18 MB
// working set from the CCD's shared L3. A fresh process without the prelude was 10% slower in the flow cell on glibc,
// which hid that cell's 1.13x.)
// Also checked: a cached block is charged at its data bytes, not the allocator's size class (make_block_cache).
// And a flow plan's rows: dedup + join + rename + project over a few thousand vid rows, the
// allocation pattern of a plan's flow steps, within the same 10% after the history. `--control` slows that cell's
// after pass on purpose (a spin of half its fresh median): the cell must then FAIL. That shows the cell can fail. The
// allocator itself: on a glibc build (-DEVALUATOR_ALLOCATOR=glibc) the flow cell
// FAILs at 1.13, twice (seek 6.6x, copy 4.2x): it catches the heap-history slowdown, by a thin 3% margin. Interleaved:
// flow 1.13 (rounds 1.11-1.13), seek 6.8x, copy 3.8x.
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <random>
#include <fcntl.h>
#include <sched.h>
#include <spawn.h>
#include <stdexcept>
#include <sys/wait.h>
#include <string>
#include <unistd.h>
#include <vector>

#include "allocator.h"
#include "ir/plan.h"
#include "ops/flow.h"
#include "store/counting.h"
#include "store/rocksdb_store.h"
#include "value/value.h"
#include "harness.h"

namespace {

using namespace evalcpp;

static const double kBound = 1.10;      // after / fresh, per step (median)
static const int kKeys = 300, kReps = 4000, kFlowReps = 150, kRounds = 15;  // odd: a true median of the rounds
static const size_t kHistoryMb = 600;

static double now_ns() {
  return std::chrono::duration<double, std::nano>(std::chrono::steady_clock::now().time_since_epoch()).count();
}
static double median(std::vector<double> v) {
  std::sort(v.begin(), v.end());
  return v[v.size() / 2];
}

// what one request allocates and frees around its reads: small rows and strings, a few mid-size buffers, and some
// value-sized buffers (freed before the next SEEK, as a request's rows are)
static void request_noise(std::mt19937_64& rng) {
  std::vector<std::string> sink;
  std::uniform_int_distribution<int> small(8, 600), mid(1100, 4000), big(5000, 90000);
  for (int i = 0; i < 120; i++) sink.emplace_back(small(rng), 'x');
  for (int i = 0; i < 6; i++) sink.emplace_back(mid(rng), 'y');
  for (int i = 0; i < 4; i++) sink.emplace_back(big(rng), 'z');
}

struct Medians { double seek_us, copy_us, flow_us; };

// one flow plan's work: dedup of 6,000 vids (a third repeated), a join of 2,000 rows to 4,000 on _vid, a rename and
// a project of the 4,000 (each output row built and freed, as a plan's steps do)
struct FlowInput { Args dd, jn, rn, pj; };
static FlowInput flow_input() {
  FlowInput in;
  ValueList vids, right, left;
  for (int i = 0; i < 6000; i++) vids.push_back(Value::vid(uint64_t(i % 4000) + 1));
  for (int i = 0; i < 4000; i++)
    right.push_back(make_row({{"_vid", Value::vid(uint64_t(i) + 1)}, {"n", Value(int64_t(i))}, {"s", Value(std::string(24, 's'))}}));
  for (int i = 0; i < 2000; i++) left.push_back(make_row({{"p", Value::vid(uint64_t(i * 2) + 1)}, {"m", Value(int64_t(i))}}));
  in.dd.set("items", Value::list(vids));
  in.jn.set("left", Value::list(left)); in.jn.set("right", Value::list(right)); in.jn.set("left_on", Value("p"));
  in.rn.set("rows", Value::list(right)); in.rn.set("names", make_row({{"n", Value("k")}}));
  in.pj.set("rows", Value::list(right)); in.pj.set("fields", Value::list({Value("s"), Value("_vid")}));
  return in;
}
static double flow_once(const FlowInput& in) {
  auto op = [](const char* name, const Args& a) { Step st; st.op = name; return run_flow_op(st, a); };
  double t0 = now_ns();
  size_t n = op("dedup", in.dd).as_list().size() + op("join", in.jn).as_list().size() +
             op("rename", in.rn).as_list().size() + op("project", in.pj).as_list().size();
  double t1 = now_ns();
  if (n != 4000 + 2000 + 4000 + 4000) throw std::runtime_error("flow rows " + std::to_string(n));
  return t1 - t0;
}

static Medians measure(CountingStore& st, std::mt19937_64& rng, const FlowInput& fin, double spin_ns) {
  std::vector<double> seek_ns, copy_ns, flow_ns;
  char key[32];
  for (int r = 0; r < kReps; r++) {
    request_noise(rng);
    snprintf(key, sizeof key, "k%06d", (int)(rng() % kKeys));
    double t0 = now_ns();
    Cursor cur = st.seek(key);
    double t1 = now_ns();
    std::string v(cur.value());
    double t2 = now_ns();
    if (v.size() != 59000) throw std::runtime_error("value size " + std::to_string(v.size()));
    seek_ns.push_back(t1 - t0);
    copy_ns.push_back(t2 - t1);
  }
  for (int r = 0; r < kFlowReps; r++) {
    request_noise(rng);
    double f = flow_once(fin);
    if (spin_ns > 0) {                  // the control: the after pass made slower on purpose
      double t0 = now_ns();
      while (now_ns() - t0 < spin_ns) {}
      f += now_ns() - t0;
    }
    flow_ns.push_back(f);
  }
  return {median(seek_ns) / 1e3, median(copy_ns) / 1e3, median(flow_ns) / 1e3};
}

// a heap history: mixed-size objects, a quarter kept alive between the freed ones so the freed pieces cannot merge
static void history(std::mt19937_64& rng, std::vector<void*>& live) {
  std::lognormal_distribution<double> sz(9.0, 1.4);
  free(malloc(4 << 20));   // glibc raises its mmap threshold after freeing a mapped chunk, as a long run does
  std::vector<void*> freed;
  for (size_t total = 0; total < kHistoryMb << 20;) {
    size_t n = std::min<size_t>(200000, std::max<double>(16, sz(rng)));
    void* p = malloc(n);
    memset(p, 1, std::min<size_t>(n, 64));
    total += n;
    (rng() % 4 == 0 ? live : freed).push_back(p);
  }
  std::shuffle(freed.begin(), freed.end(), rng);
  for (void* p : freed) free(p);
}

// the test's own temp store (~18 MB), removed on every exit path
static int run(const std::string& dir, bool control);
static int fresh_process(const std::string& dir, int cpu);

// what both sides do before measuring (the fresh side then skips only the history): a store of kKeys 59 KB values,
// written and flushed in this process, read through its read-write handle; and the cache charge of one more block
struct Prelude { std::unique_ptr<Store> store; double charge; };
static Prelude prelude(const std::string& dir, std::mt19937_64& rng) {
  StoreOptions so;
  Prelude p{Store::open_read_write(dir, so, true), 0};
  std::string val(59000, 'v');
  char key[32];
  for (int i = 0; i < kKeys; i++) {
    snprintf(key, sizeof key, "k%06d", i);
    for (int j = 0; j < 8; j++) val[j * 7000] = (char)rng();
    if (!p.store->db()->Put(rocksdb::WriteOptions(), key, val).ok()) throw std::runtime_error("put");
  }
  p.store->db()->Flush(rocksdb::FlushOptions());
  // the block cache's charge for one more 59 KB block (the first read loads the index / filter blocks too)
  uint64_t u0 = 0, u1 = 0, u2 = 0;
  std::string v;
  p.store->db()->GetIntProperty("rocksdb.block-cache-usage", &u0);
  p.store->db()->Get(rocksdb::ReadOptions(), "k000000", &v);
  p.store->db()->GetIntProperty("rocksdb.block-cache-usage", &u1);
  p.store->db()->Get(rocksdb::ReadOptions(), "k000001", &v);
  p.store->db()->GetIntProperty("rocksdb.block-cache-usage", &u2);
  p.charge = double(u2 - u1);
  return p;
}

static void pin(int cpu) {             // one CPU: a move between NUMA nodes mid-test changes the copy cost
  cpu_set_t one;
  CPU_ZERO(&one);
  CPU_SET(cpu, &one);
  sched_setaffinity(0, sizeof one, &one);
}

// the fresh process, seen from the parent: one measured pass per request ('M' on its fd 3, the medians back on fd 4)
struct FreshProcess {
  pid_t pid = -1;
  FILE* to = nullptr;
  FILE* from = nullptr;
  FreshProcess(const std::string& dir, int cpu) {
    int p2c[2], c2p[2];
    if (pipe2(p2c, O_CLOEXEC) || pipe2(c2p, O_CLOEXEC)) throw std::runtime_error("pipe");
    for (int* fd : {&p2c[0], &p2c[1], &c2p[0], &c2p[1]}) {   // above 4, so the dup2 onto 3 and 4 always copies
      int moved = fcntl(*fd, F_DUPFD_CLOEXEC, 10);
      close(*fd);
      *fd = moved;
    }
    posix_spawn_file_actions_t fa;
    posix_spawn_file_actions_init(&fa);
    posix_spawn_file_actions_adddup2(&fa, p2c[0], 3);
    posix_spawn_file_actions_adddup2(&fa, c2p[1], 4);
    std::string self = std::filesystem::read_symlink("/proc/self/exe"), cpus = std::to_string(cpu);
    char* argv[] = {self.data(), (char*)"test_latency_stability", (char*)"--fresh", (char*)dir.c_str(), cpus.data(), nullptr};
    int rc = posix_spawn(&pid, self.c_str(), &fa, nullptr, argv, environ);
    posix_spawn_file_actions_destroy(&fa);
    close(p2c[0]);
    close(c2p[1]);
    if (rc) throw std::runtime_error("posix_spawn the fresh process: " + std::string(strerror(rc)));
    to = fdopen(p2c[1], "w");
    from = fdopen(c2p[0], "r");
    char ready = 0;                      // its prelude done
    if (fscanf(from, " %c", &ready) != 1 || ready != 'R') throw std::runtime_error("the fresh process did not start");
  }
  Medians pass() {
    Medians m{};
    fputs("M\n", to);
    fflush(to);
    if (fscanf(from, "%lf %lf %lf", &m.seek_us, &m.copy_us, &m.flow_us) != 3)
      throw std::runtime_error("the fresh process gave no medians (it died?)");
    return m;
  }
  ~FreshProcess() {                    // EOF on its fd 3 ends it
    if (to) fclose(to);
    if (from) fclose(from);
    int st = 0;
    if (pid > 0) waitpid(pid, &st, 0);
  }
};

static int fresh_process(const std::string& dir, int cpu) {
  pin(cpu);
  std::mt19937_64 rng(7);
  Prelude p = prelude(dir, rng);       // its own store, built as the parent built its own
  CountingStore st(*p.store);
  FlowInput fin = flow_input();
  FILE* in = fdopen(3, "r");
  FILE* out = fdopen(4, "w");
  fputs("R\n", out);
  fflush(out);
  char cmd;
  while (fscanf(in, " %c", &cmd) == 1 && cmd == 'M') {
    Medians m = measure(st, rng, fin, 0);
    fprintf(out, "%.6f %.6f %.6f\n", m.seek_us, m.copy_us, m.flow_us);
    fflush(out);
  }
  return 0;
}

struct TempDir {
  std::string path;
  ~TempDir() { if (!path.empty()) std::filesystem::remove_all(path); }
};

TEST_CASE(test_latency_stability) {
  allocator_init();
  if (argc == 4 && std::string(argv[1]) == "--fresh") {
    try {
      std::exit(fresh_process(argv[2], std::atoi(argv[3])));   // the child runs no check: its status is its own, not the harness's
    } catch (const std::exception& e) {
      std::cerr << "FAIL (fresh process) " << e.what() << "\n";
      return 1;
    }
  }
  bool control = argc > 1 && std::string(argv[1]) == "--control";
  pin(sched_getcpu());
  char tmpl[] = "/tmp/evalcpp-stability-XXXXXX";
  if (!kvt::temp_dir(tmpl)) { std::cerr << "FAIL mkdtemp\n"; return 2; }
  TempDir tmp{tmpl};
  try {
    return run(tmp.path, control);
  } catch (const std::exception& e) {
    std::cerr << "FAIL " << e.what() << "\n";
    return 1;
  }
}

static int run(const std::string& dir, bool control) {
  std::mt19937_64 rng(7);
  Prelude p = prelude(dir + "/after", rng);
  double charge = p.charge;
  CountingStore st(*p.store);
  int cpu = sched_getcpu();
  FreshProcess fresh(dir + "/fresh", cpu);   // the same prelude in its own process and store
  FlowInput fin = flow_input();
  measure(st, rng, fin, 0);            // this side's blocks are cached before the history, as in a long run
  Medians warm = fresh.pass();         // the fresh side's warm-up
  std::vector<void*> live;
  history(rng, live);
  double spin_ns = control ? warm.flow_us * 1e3 * 0.5 : 0;
  measure(st, rng, fin, spin_ns);      // the after side's warm-up
  std::vector<double> f_seek, f_copy, f_flow, a_seek, a_copy, a_flow, r_seek, r_copy, r_flow;
  for (int r = 0; r < kRounds; r++) {
    Medians f, a;
    if (r % 2 == 0) {
      f = fresh.pass();
      a = measure(st, rng, fin, spin_ns);
    } else {
      a = measure(st, rng, fin, spin_ns);
      f = fresh.pass();
    }
    f_seek.push_back(f.seek_us), f_copy.push_back(f.copy_us), f_flow.push_back(f.flow_us);
    a_seek.push_back(a.seek_us), a_copy.push_back(a.copy_us), a_flow.push_back(a.flow_us);
    r_seek.push_back(a.seek_us / f.seek_us), r_copy.push_back(a.copy_us / f.copy_us), r_flow.push_back(a.flow_us / f.flow_us);
  }
  for (void* p : live) free(p);

  auto cell = [&](const char* step, const std::vector<double>& f, const std::vector<double>& a,
                  const std::vector<double>& ratio) {
    double rm = median(ratio), lo = *std::min_element(ratio.begin(), ratio.end()),
           hi = *std::max_element(ratio.begin(), ratio.end());
    bool ok = rm <= kBound;
    CHECK(ok, step << ": the median round's after / fresh " << rm << " > " << kBound);
    printf("[stability] %-38s fresh %6.2f us  after %6.2f us  ratio %.2f (rounds %.2f-%.2f)  %s\n", step, median(f),
           median(a), rm, lo, hi, ok ? "ok" : "FAIL");
  };
  printf("[stability] allocator %s; fresh and after in two processes on CPU %d, %d rounds of alternate passes (%d reps "
         "each); history %zu MB; bound: the median round's after / fresh <= %.2f\n",
         allocator_info().value("name", "?").c_str(), cpu, kRounds, kReps, kHistoryMb, kBound);
  cell("seek (new iterator + Seek, 59 KB value)", f_seek, a_seek, r_seek);
  cell("copy of the 59 KB value", f_copy, a_copy, r_copy);
  cell(control ? "flow plan rows (CONTROL: after slowed)" : "flow plan rows (dedup, join, rename, project)",
       f_flow, a_flow, r_flow);
  // data bytes (59,000 + key + block trailer + the cache's own handle) vs tcmalloc's 64 KB size class (+11%)
  bool charge_ok = charge >= 59000 && charge <= 59000 * 1.03;
  CHECK(charge_ok, "block-cache charge " << charge << " B for a 59,000 B value");
  printf("[stability] %-38s %.0f B for a 59,000 B value (bound: <= 1.03x)  %s\n", "block-cache charge of one block",
         charge, charge_ok ? "ok" : "FAIL");
  printf("[stability] 4 cells (3 steps x fresh / after, 1 cache charge), %d failed\n", fails);
  p.store.reset();
  return fails ? 1 : 0;
}

}  // namespace
