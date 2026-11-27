// The one chunk-cut rule of every Chunked layout (meta, pertype_csr, the packed posting index): greedy close-after.
// Units (a vertex row, an owner's list, an edge-prop row, an index term) are added in key order with their estimated
// size; a chunk closes once its size reaches the target, the unit that crosses it included. A caller closes the last
// chunk at the end of its run and wherever its layout forbids a chunk to span (meta: a vid group).
#pragma once
#include <cstdint>

namespace evalcpp {

class GreedyChunker {
 public:
  explicit GreedyChunker(uint64_t target) : target_(target) {}
  // add one unit; true = the chunk is full and closes after this unit (its size starts again at 0)
  bool add(uint64_t est) {
    size_ += est;
    if (size_ < target_) return false;
    size_ = 0;
    return true;
  }
  void reset() { size_ = 0; }   // the caller closed the chunk for another reason (end of run, a group boundary)

 private:
  uint64_t target_;
  uint64_t size_ = 0;
};

}  // namespace evalcpp
