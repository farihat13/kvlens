// `run <request.json|->`: one request, one response on stdout.
// `serve`: JSON lines -- one request per stdin line, one response per stdout line; the store
// stays open across requests (a WARM cache persists; COLD reopens in-process).
#include <fstream>
#include <iostream>
#include <sstream>

#include "cli/commands.h"
#include "cli/session.h"

namespace evalcpp {

int cmd_run(const std::vector<std::string>& args) {
  if (args.empty()) {
    std::cerr << "usage: kvevaluator run <request.json | ->\n";
    return 2;
  }
  json req;
  if (args[0] == "-") req = json::parse(std::cin);
  else {
    std::ifstream f(args[0]);
    if (!f) { std::cerr << "cannot open " << args[0] << "\n"; return 1; }
    req = json::parse(f);
  }
  Session s;
  json resp = s.handle(req);
  std::cout << resp.dump() << "\n";
  if (!resp["error"].is_null()) {
    std::cerr << "error (" << resp["error"]["where"].get<std::string>() << "): "
              << resp["error"]["what"].get<std::string>() << "\n";
    return 1;
  }
  return 0;
}

int cmd_serve(const std::vector<std::string>& args) {
  // C++ streams not synced with C stdio: std::getline on a synced std::cin reads a char at a time (~39 us of a
  // request's wall at sf0.1 for a ~2 KB line); serve uses only the C++ streams, so nothing interleaves wrongly
  std::ios::sync_with_stdio(false);
  Session s;
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.empty()) continue;
    json resp;
    try {
      json req = json::parse(line);
      resp = s.handle(req);
    } catch (const std::exception& e) {
      resp = {{"rows", nullptr}, {"error", {{"where", "request"}, {"what", e.what()}}}};
    }
    std::cout << resp.dump() << "\n" << std::flush;
  }
  return 0;
}

}  // namespace evalcpp
