// The vertex stream's one rule, shared by every format's ingest: records arrive grouped by label,
// one label at a time; a label that comes back after its rows were flushed is refused.
#pragma once
#include <set>
#include <stdexcept>
#include <string>

namespace evalcpp {

class LabelStream {
 public:
  explicit LabelStream(std::string who) : who_(std::move(who)) {}
  bool changes(const std::string& label) const { return label != cur_; }   // the caller flushes the old label first
  void start(const std::string& label) {
    if (flushed_.count(label))
      throw std::runtime_error(who_ + " ingest: label " + label + " reappears after its rows were flushed "
                               "(the record stream must group vertices by label)");
    cur_ = label;
  }
  void finish() {                                                          // the current label's rows are flushed
    if (!cur_.empty()) flushed_.insert(cur_);
    cur_.clear();
  }
  bool open() const { return !cur_.empty(); }
  const std::string& label() const { return cur_; }

 private:
  std::string who_;
  std::string cur_;
  std::set<std::string> flushed_;
};

}  // namespace evalcpp
