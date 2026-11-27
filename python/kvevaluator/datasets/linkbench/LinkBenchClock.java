package com.facebook.LinkBench;

// LinkBenchClock: the clock LinkBench's generators stamp into the data. LinkBench reads the wall clock for every link
// and node time, so two generations never gave the same bytes. generate.sh puts now() in place of those reads
// (LinkBenchLoad, NodeLoader, LinkBenchRequest) and FileDumpGraphStore calls at() as it writes a line.
//   -Dlinkbench.clock=<ms>        REQUIRED: the one value now() returns. No setting = no generation (never the wall clock).
//   -Dlinkbench.clocktrace=<csv>  optional: a recorded clock, as steps per output file: `file,unit,first_line,clock`
//                                 = "from this line of <file> on (0-based), a time field in <unit> (ms | s) was stamped
//                                 when the clock read <clock>". at() moves a line's time from the fixed value to the
//                                 recorded one. Keyed by the output LINE, never by how many calls came before, so a
//                                 replay does not depend on thread scheduling.
// Written for the kvformat evaluator (python/kvevaluator/datasets/linkbench/); not part of LinkBench.

import java.io.BufferedReader;
import java.io.FileReader;
import java.io.IOException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Map;

public final class LinkBenchClock {
  private static final long FIXED = fixed();
  private static final Map<String, long[][]> STEPS = steps();     // "<file>,<unit>" -> {first lines, clock values}
  private static final Map<String, Integer> CURSOR = new HashMap<>();

  private LinkBenchClock() { }

  private static long fixed() {
    String v = System.getProperty("linkbench.clock");
    if (v == null || v.trim().isEmpty())
      throw new IllegalStateException("-Dlinkbench.clock=<ms> is required: generated data never takes the wall clock");
    return Long.parseLong(v.trim());
  }

  private static Map<String, long[][]> steps() {
    Map<String, long[][]> out = new HashMap<>();
    String path = System.getProperty("linkbench.clocktrace");
    if (path == null || path.trim().isEmpty()) return out;
    Map<String, ArrayList<long[]>> rows = new HashMap<>();
    try (BufferedReader r = new BufferedReader(new FileReader(path.trim()))) {
      String line = r.readLine();
      if (line == null || !line.trim().equals("file,unit,first_line,clock"))
        throw new IllegalStateException(path + ": not a clock trace (header file,unit,first_line,clock)");
      while ((line = r.readLine()) != null) {
        if (line.trim().isEmpty()) continue;
        String[] f = line.trim().split(",");
        if (f.length != 4 || !(f[1].equals("ms") || f[1].equals("s")))
          throw new IllegalStateException(path + ": bad row: " + line);
        rows.computeIfAbsent(f[0] + "," + f[1], k -> new ArrayList<>()).add(new long[] {Long.parseLong(f[2]), Long.parseLong(f[3])});
      }
    } catch (IOException e) {
      throw new IllegalStateException("clock trace " + path + ": " + e);
    }
    for (Map.Entry<String, ArrayList<long[]>> e : rows.entrySet()) {
      ArrayList<long[]> v = e.getValue();
      long[][] s = new long[2][v.size()];
      for (int i = 0; i < v.size(); i++) {
        s[0][i] = v.get(i)[0];
        s[1][i] = v.get(i)[1];
        if (i > 0 && s[0][i] <= s[0][i - 1]) throw new IllegalStateException(path + ": " + e.getKey() + " lines do not rise at " + s[0][i]);
      }
      out.put(e.getKey(), s);
    }
    return out;
  }

  /** the clock every generator reads: the fixed value */
  public static long now() { return FIXED; }

  /** the time to write on line `line` (0-based) of `file` for a field stamped with now(): the same value without a
   *  trace; with one, the recorded clock in the fixed one's place. Call with a file's lines in rising order, under the
   *  writer's lock. A line the trace does not cover stops the run. */
  public static long at(String file, boolean seconds, long line, long time) {
    if (STEPS.isEmpty()) return time;
    String key = file + "," + (seconds ? "s" : "ms");
    long[][] s = STEPS.get(key);
    if (s == null || s[0].length == 0 || line < s[0][0])
      throw new IllegalStateException("the clock trace has no step for " + key + " at line " + line);
    int i = CURSOR.getOrDefault(key, 0);
    if (s[0][i] > line) i = 0;                               // a file's lines come in rising order; start over if not
    while (i + 1 < s[0].length && s[0][i + 1] <= line) i++;
    CURSOR.put(key, i);
    return time - (seconds ? FIXED / 1000 : FIXED) + s[1][i];
  }
}
