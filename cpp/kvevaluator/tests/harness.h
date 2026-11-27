// The unit tests' one harness: a case registers by name (TEST_CASE), its checks count (CHECK, CHECK_EQ), and
// harness_main.cc runs one case per process and prints `[<case>] checks=N fails=M`; a case that ran no check fails.
// Each test file keeps its helpers in an anonymous namespace: all cases link into one binary (kvevaluator_tests).
#pragma once
#include <cstdio>
#include <iostream>
#include <map>
#include <sstream>
#include <string>
#include <string_view>

#include "formats/format.h"
#include "repo_paths.h"

namespace kvt {

inline int checks = 0;     // checks run by this process's case
inline int fails = 0;      // checks that failed
inline bool keep_tmp = false;   // `kvevaluator_tests <case> --keep-tmp`: keep the case's temp dirs (to debug it)

using CaseFn = int (*)(int argc, char** argv);
void register_case(const char* name, CaseFn fn);

// one failed check: counted, and its first 40 printed per case (a broken loop prints 40 lines, not 40,000)
inline void fail_at(const char* file, int line, const char* cond, const std::string& msg) {
  if (fails++ < 40) {
    std::cerr << "FAIL " << file << ":" << line << ": " << cond;
    if (!msg.empty()) std::cerr << ": " << msg;
    std::cerr << "\n";
  }
}

// mkdtemp's contract (fills the template, returns it or nullptr); the dir is removed when the case ends (--keep-tmp keeps)
char* temp_dir(char* tmpl);

// a case that cannot run here (one usable CPU): its return value; ctest reports it skipped (SKIP_RETURN_CODE 77)
inline int skip(const std::string& why) {
  std::cout << "SKIPPED: " << why << "\n";
  return 77;
}

// an in-memory sink: every put and index key kept (an index key with an empty value)
struct MemSink : evalcpp::KvSink {
  std::map<std::string, std::string> kv;
  void begin_run(const std::string&) override {}
  void put(std::string_view k, std::string_view v) override { kv[std::string(k)] = std::string(v); }
  void end_run() override {}
  void put_index(int, std::string key) override { kv[key] = ""; }
};

struct Registrar {
  Registrar(const char* name, CaseFn fn) { register_case(name, fn); }
};

}  // namespace kvt

using kvt::checks;   // the names the tests print and return on
using kvt::fails;

#define KVT_CHECK1(cond) KVT_CHECK2(cond, "")
#define KVT_CHECK2(cond, msg)                                                         \
  do {                                                                                \
    ::kvt::checks++;                                                                  \
    if (!(cond)) {                                                                    \
      std::ostringstream kvt_msg_;                                                    \
      kvt_msg_ << msg;                                                                \
      ::kvt::fail_at(__FILE__, __LINE__, #cond, kvt_msg_.str());                      \
    }                                                                                 \
  } while (0)
#define KVT_PICK(_1, _2, NAME, ...) NAME
// CHECK(cond) or CHECK(cond, msg): msg is a stream expression ("x " << x)
#define CHECK(...) KVT_PICK(__VA_ARGS__, KVT_CHECK2, KVT_CHECK1, _)(__VA_ARGS__)
#define CHECK_EQ(a, b)                                                                \
  do {                                                                                \
    ::kvt::checks++;                                                                  \
    auto kvt_a_ = (a);                                                                \
    auto kvt_b_ = (b);                                                                \
    if (!(kvt_a_ == kvt_b_)) {                                                        \
      std::ostringstream kvt_msg_;                                                    \
      kvt_msg_ << #a " = " << kvt_a_ << ", want " #b " = " << kvt_b_;                 \
      ::kvt::fail_at(__FILE__, __LINE__, #a " == " #b, kvt_msg_.str());               \
    }                                                                                 \
  } while (0)

// TEST_CASE(test_x) { ... return rc; }: argc / argv as a main's (argv[0] the binary, then the case's own arguments).
// CMake reads the names from `^TEST_CASE(<name>)` lines: one ctest test per case.
#define TEST_CASE(name)                                                               \
  static int name##_case(int argc, char** argv);                                      \
  static ::kvt::Registrar name##_registrar(#name, name##_case);                       \
  static int name##_case(int argc, char** argv)
