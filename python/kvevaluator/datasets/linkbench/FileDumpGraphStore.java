package com.facebook.LinkBench;

// FileDumpGraphStore: a LinkBench store that EXPORTS instead of storing, so LinkBench's own generators drive our
// evaluator. Set `linkstore`/`nodestore` to this class and pass -Dlinkbench.dumpdir=<dir>.
//   LOAD phase (`-l`):    the generated graph -> <dir>/nodes.jsonl + links.jsonl (byte blobs base64-encoded).
//   REQUEST phase (`-r`): every request LinkBench issues -> <dir>/requests.jsonl, one call per line: the op and its
//                         ids, the link type, and for writes the new field values. Seed it (request_random_seed)
//                         and the log is the workload's params: LinkBench's own op mix and access distributions.
// Reads return nothing (a null node, empty lists, count 0): the request phase only needs the calls. So
// LinkBench never builds its getLinkList history (it keeps full-page results), and every getLinkList takes the
// plain path -- in the layouts we measure, a history page costs the same as a plain one.
// addLink is LinkBench's UPSERT for both its addLink and updateLink ops (the store cannot tell them apart); the
// analysis classifies each by whether (id1, link_type, id2) already exists in the loaded data.
// Every time field is written through LinkBenchClock.at(file, unit, line, time): the generators stamp one fixed clock
// value (never the wall clock), and a recorded clock, when given, takes its place per output line (LinkBenchClock.java).
// Written for the kvformat evaluator (python/kvevaluator/datasets/linkbench/); not part of LinkBench.

import java.io.BufferedWriter;
import java.io.FileWriter;
import java.io.IOException;
import java.io.PrintWriter;
import java.util.Base64;
import java.util.List;
import java.util.Properties;

public class FileDumpGraphStore extends GraphStore {
  private static PrintWriter NODES, LINKS, REQUESTS;
  private static long nextNodeId = -1;   // ids for nodes added in the REQUEST phase: maxid1, maxid1 + 1, ...
  private static long nodeLines = 0, linkLines = 0, requestLines = 0;   // lines written so far, per file (under the class lock)

  private static PrintWriter writer(String name) throws IOException {
    String dir = System.getProperty("linkbench.dumpdir", ".");
    return new PrintWriter(new BufferedWriter(new FileWriter(dir + "/" + name)), true);
  }

  // Open ONLY the current phase's files: the request phase must never truncate the loaded graph's JSONL.
  private static synchronized void open(Phase phase, Properties p) {
    try {
      if (phase == Phase.REQUEST) {
        if (REQUESTS != null) return;
        REQUESTS = writer("requests.jsonl");
        nextNodeId = Long.parseLong(p.getProperty("maxid1").trim());
      } else {
        if (NODES != null) return;
        NODES = writer("nodes.jsonl");
        LINKS = writer("links.jsonl");
      }
    } catch (IOException e) {
      throw new RuntimeException(e);
    }
    Runtime.getRuntime().addShutdownHook(new Thread(() -> {
      synchronized (FileDumpGraphStore.class) {
        for (PrintWriter w : new PrintWriter[] {NODES, LINKS, REQUESTS})
          if (w != null) { w.flush(); w.close(); }
      }
    }));
  }

  private static String b64(byte[] d) {
    return d == null ? "" : Base64.getEncoder().encodeToString(d);
  }

  private static void request(String json) {
    synchronized (FileDumpGraphStore.class) {
      REQUESTS.println(json);
      requestLines++;
    }
  }

  private static void requestLink(String op, Link a) {       // a request that carries a link's time (ms)
    synchronized (FileDumpGraphStore.class) {
      request("{\"op\":\"" + op + "\"," + linkFields(a, LinkBenchClock.at("requests.jsonl", false, requestLines, a.time)) + "}");
    }
  }

  private static void requestNode(String op, Node n) {       // a request that carries a node's time (seconds)
    synchronized (FileDumpGraphStore.class) {
      request("{\"op\":\"" + op + "\"," + nodeFields(n, LinkBenchClock.at("requests.jsonl", true, requestLines, n.time)) + "}");
    }
  }

  private static String linkFields(Link a, long time) {
    return "\"id1\":" + a.id1 + ",\"link_type\":" + a.link_type + ",\"id2\":" + a.id2
        + ",\"visibility\":" + a.visibility + ",\"version\":" + a.version + ",\"time\":" + time
        + ",\"data\":\"" + b64(a.data) + "\"";
  }

  private static String nodeFields(Node n, long time) {
    return "\"id\":" + n.id + ",\"type\":" + n.type + ",\"version\":" + n.version
        + ",\"time\":" + time + ",\"data\":\"" + b64(n.data) + "\"";
  }

  // ---- lifecycle ----
  @Override public void initialize(Properties p, Phase currentPhase, int threadId) { open(currentPhase, p); }
  @Override public void close() { /* no-op; the shutdown hook flushes+closes the shared writers */ }
  @Override public void clearErrors(int threadID) { }
  @Override public void resetNodeStore(String dbid, long startID) { }
  @Override public int bulkLoadBatchSize() { return 0; }   // links flow through addLink (per item)

  // ---- writes: EXPORT (load) or LOG (request) ----
  @Override public boolean addLink(String dbid, Link a, boolean noinverse) {
    if (REQUESTS != null) { requestLink("upsert_link", a); return false; }
    synchronized (FileDumpGraphStore.class) {
      LINKS.println("{" + linkFields(a, LinkBenchClock.at("links.jsonl", false, linkLines, a.time)) + "}");
      linkLines++;
    }
    return true;
  }

  @Override public long addNode(String dbid, Node node) {
    if (REQUESTS != null) {
      synchronized (FileDumpGraphStore.class) { node.id = nextNodeId++; }
      requestNode("add_node", node);
      return node.id;
    }
    synchronized (FileDumpGraphStore.class) {
      NODES.println("{" + nodeFields(node, LinkBenchClock.at("nodes.jsonl", true, nodeLines, node.time)) + "}");
      nodeLines++;
    }
    return node.id;
  }

  @Override public long[] bulkAddNodes(String dbid, List<Node> nodes) {
    long[] ids = new long[nodes.size()];
    for (int i = 0; i < nodes.size(); i++) ids[i] = addNode(dbid, nodes.get(i));
    return ids;
  }

  @Override public boolean deleteLink(String dbid, long id1, long lt, long id2, boolean noinverse, boolean expunge) {
    request("{\"op\":\"delete_link\",\"id1\":" + id1 + ",\"link_type\":" + lt + ",\"id2\":" + id2
        + ",\"expunge\":" + expunge + "}");
    return false;
  }

  @Override public boolean updateLink(String dbid, Link a, boolean noinverse) {   // unused by LinkBenchRequest (it upserts)
    requestLink("update_link", a);
    return false;
  }

  @Override public boolean updateNode(String dbid, Node node) {
    requestNode("update_node", node);
    return false;
  }

  @Override public boolean deleteNode(String dbid, int type, long id) {
    request("{\"op\":\"delete_node\",\"id\":" + id + "}");
    return false;
  }

  // ---- reads: LOG (request) ----
  @Override public Node getNode(String dbid, int type, long id) {
    request("{\"op\":\"get_node\",\"id\":" + id + "}");
    return null;
  }

  @Override public Link getLink(String dbid, long id1, long lt, long id2) {
    request("{\"op\":\"get_link\",\"id1\":" + id1 + ",\"link_type\":" + lt + ",\"id2s\":[" + id2 + "]}");
    return null;
  }

  @Override public Link[] multigetLinks(String dbid, long id1, long lt, long[] id2s) {
    StringBuilder ids = new StringBuilder();
    for (int i = 0; i < id2s.length; i++) ids.append(i == 0 ? "" : ",").append(id2s[i]);
    request("{\"op\":\"get_link\",\"id1\":" + id1 + ",\"link_type\":" + lt + ",\"id2s\":[" + ids + "]}");
    return new Link[0];
  }

  @Override public Link[] getLinkList(String dbid, long id1, long lt) {
    request("{\"op\":\"get_link_list\",\"id1\":" + id1 + ",\"link_type\":" + lt + ",\"limit\":" + getRangeLimit() + "}");
    return new Link[0];
  }

  @Override public Link[] getLinkList(String dbid, long id1, long lt, long minTs, long maxTs, int off, int lim) {
    request("{\"op\":\"get_link_list\",\"id1\":" + id1 + ",\"link_type\":" + lt + ",\"min_time\":" + minTs
        + ",\"max_time\":" + maxTs + ",\"offset\":" + off + ",\"limit\":" + lim + "}");
    return new Link[0];
  }

  @Override public long countLinks(String dbid, long id1, long lt) {
    request("{\"op\":\"count_links\",\"id1\":" + id1 + ",\"link_type\":" + lt + "}");
    return 0;
  }
}
