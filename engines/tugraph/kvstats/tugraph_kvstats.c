/* tugraph_kvstats: count TuGraph's calls into LMDB, with no change to TuGraph.
 *
 * TuGraph v4.5.2 keeps LMDB inside liblgraph.so and calls it through the dynamic linker (the library is
 * not linked -Bsymbolic), so a library loaded first (LD_PRELOAD) receives every call TuGraph makes to
 * the LMDB API, counts it, and passes it on. Calls LMDB makes to itself (mdb_put -> mdb_cursor_put,
 * a commit's free-list writes) arrive here too; a per-thread depth keeps them out: only the outermost
 * call, the one TuGraph made, is counted.
 *
 * What a call counts as (the KV boundary):
 *   mdb_get                                    GET
 *   mdb_cursor_get  MDB_SET, MDB_SET_KEY       GET      an exact-key point lookup
 *   mdb_cursor_get  MDB_SET_RANGE, FIRST, LAST SEEK
 *   mdb_cursor_get  MDB_NEXT                   NEXT
 *   mdb_cursor_get  MDB_PREV                   PREV     (a reader adds it to NEXT)
 *   mdb_cursor_get  MDB_GET_CURRENT            its own column: no movement in the tree, in no total
 *   mdb_put, mdb_cursor_put                    PUT
 *   mdb_del, mdb_cursor_del                    DELETE
 *   mdb_txn_commit                             COMMIT
 * Bytes are the MDB_val sizes at the call: the key and the value LMDB returned (a read that found its
 * key) or was given (a write); a read that found nothing counts its probe key. A value's size is what
 * LMDB stores, so it includes the 8-byte version TuGraph puts before every value.
 *
 * Read and reset over HTTP on 127.0.0.1:$TUGRAPH_KVSTATS_PORT (no port, no listener):
 *   GET /kvstats          one JSON object: per op <op>_count, <op>_miss_count, <op>_key_bytes,
 *                         <op>_value_bytes, <op>_bytes, <op>_latency_ns; commit_count; and the raw
 *                         channels "by_call" (per LMDB call) and "by_table" (per LMDB table and call)
 *   PUT /kvstats/reset    every counter to zero
 *
 * Build: engines/tugraph/build.sh.   The cursor op codes below are LMDB's (src/core/lmdb/lmdb.h at the tag).
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

typedef struct MDB_env MDB_env;
typedef struct MDB_txn MDB_txn;
typedef struct MDB_cursor MDB_cursor;
typedef unsigned int MDB_dbi;
typedef struct MDB_val { size_t mv_size; void *mv_data; } MDB_val;

enum { MDB_FIRST = 0, MDB_GET_CURRENT = 4, MDB_LAST = 6, MDB_NEXT = 8, MDB_PREV = 12, MDB_SET = 15,
       MDB_SET_KEY = 16, MDB_SET_RANGE = 17 };
#define MDB_NOTFOUND (-30798)

/* one row per LMDB call kind: the raw channel */
enum { C_GET, C_CUR_SET_KEY, C_CUR_SET_RANGE, C_CUR_FIRST, C_CUR_LAST, C_CUR_NEXT, C_CUR_PREV, C_CUR_GET_CURRENT,
       C_CUR_OTHER, C_PUT, C_CUR_PUT, C_DEL, C_CUR_DEL, N_CALLS };
static const char *CALL_NAME[N_CALLS] = {
    "mdb_get", "cursor_set_key", "cursor_set_range", "cursor_first", "cursor_last", "cursor_next", "cursor_prev",
    "cursor_get_current", "cursor_other", "mdb_put", "cursor_put", "mdb_del", "cursor_del"};
/* the op each call counts as */
enum { OP_GET, OP_SEEK, OP_NEXT, OP_PREV, OP_GET_CURRENT, OP_OTHER, OP_PUT, OP_DEL, N_OPS };
static const char *OP_NAME[N_OPS] = {"get", "seek", "next", "prev", "get_current", "other", "put", "del"};
static const int CALL_OP[N_CALLS] = {OP_GET, OP_GET, OP_SEEK, OP_SEEK, OP_SEEK, OP_NEXT, OP_PREV, OP_GET_CURRENT,
                                     OP_OTHER, OP_PUT, OP_PUT, OP_DEL, OP_DEL};

typedef struct { uint64_t count, miss, key_bytes, value_bytes, ns; } Cell;
#define MAX_TABLES 512
#define MAX_ENVS 16
#define MAX_DBI 2048
typedef struct { char name[160]; Cell cell[N_CALLS]; } Table;
static Table g_table[MAX_TABLES];
static int g_ntables;
static struct { MDB_env *env; char label[96]; int16_t slot[MAX_DBI]; } g_env[MAX_ENVS];
static int g_nenvs;
static uint64_t g_commit_count, g_commit_ns;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static __thread int t_depth;

static int (*r_get)(MDB_txn *, MDB_dbi, MDB_val *, MDB_val *);
static int (*r_put)(MDB_txn *, MDB_dbi, MDB_val *, MDB_val *, unsigned int);
static int (*r_del)(MDB_txn *, MDB_dbi, MDB_val *, MDB_val *);
static int (*r_cursor_get)(MDB_cursor *, MDB_val *, MDB_val *, int);
static int (*r_cursor_put)(MDB_cursor *, MDB_val *, MDB_val *, unsigned int);
static int (*r_cursor_del)(MDB_cursor *, unsigned int);
static int (*r_txn_commit)(MDB_txn *);
static int (*r_dbi_open)(MDB_txn *, const char *, unsigned int, MDB_dbi *);
static MDB_env *(*r_txn_env)(MDB_txn *);
static MDB_txn *(*r_cursor_txn)(MDB_cursor *);
static MDB_dbi (*r_cursor_dbi)(MDB_cursor *);
static int (*r_env_get_path)(MDB_env *, const char **);

#define ADD(x, v) __atomic_fetch_add(&(x), (v), __ATOMIC_RELAXED)

static void *real(const char *name) {
    void *p = dlsym(RTLD_NEXT, name);
    if (!p) { fprintf(stderr, "tugraph_kvstats: no %s after this library; is liblgraph.so loaded?\n", name); abort(); }
    return p;
}

static void resolve(void) {
    r_get = real("mdb_get"); r_put = real("mdb_put"); r_del = real("mdb_del");
    r_cursor_get = real("mdb_cursor_get"); r_cursor_put = real("mdb_cursor_put"); r_cursor_del = real("mdb_cursor_del");
    r_txn_commit = real("mdb_txn_commit"); r_dbi_open = real("mdb_dbi_open"); r_txn_env = real("mdb_txn_env");
    r_cursor_txn = real("mdb_cursor_txn"); r_cursor_dbi = real("mdb_cursor_dbi");
    r_env_get_path = real("mdb_env_get_path");
}
static pthread_once_t g_once = PTHREAD_ONCE_INIT;
#define READY() pthread_once(&g_once, resolve)

static uint64_t now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000ull + (uint64_t)t.tv_nsec;
}

/* the env's index; a new env is named by the last part of its directory (a graph's directory, or .meta) */
static int env_index(MDB_env *env) {
    int n = __atomic_load_n(&g_nenvs, __ATOMIC_ACQUIRE);
    for (int i = 0; i < n; i++) if (g_env[i].env == env) return i;
    pthread_mutex_lock(&g_lock);
    int i;
    for (i = 0; i < g_nenvs; i++) if (g_env[i].env == env) break;
    if (i == g_nenvs && g_nenvs < MAX_ENVS) {
        const char *path = NULL;
        r_env_get_path(env, &path);
        const char *base = path ? strrchr(path, '/') : NULL;
        snprintf(g_env[i].label, sizeof g_env[i].label, "%s", base ? base + 1 : (path ? path : "env"));
        memset(g_env[i].slot, 0xff, sizeof g_env[i].slot);
        g_env[i].env = env;
        __atomic_store_n(&g_nenvs, g_nenvs + 1, __ATOMIC_RELEASE);
    }
    pthread_mutex_unlock(&g_lock);
    return i < MAX_ENVS ? i : -1;
}

/* the table's row; `name` is given by mdb_dbi_open, else the table is named by its number */
static int table_slot(MDB_env *env, MDB_dbi dbi, const char *name, int named) {
    int e = env_index(env);
    if (e < 0 || dbi >= MAX_DBI) return 0;
    int s = __atomic_load_n(&g_env[e].slot[dbi], __ATOMIC_ACQUIRE);
    if (s >= 0 && !named) return s;
    char full[160];
    if (named) snprintf(full, sizeof full, "%s:%s", g_env[e].label, name ? name : "(main)");
    else snprintf(full, sizeof full, "%s:dbi%u", g_env[e].label, dbi);
    if (s >= 0 && strcmp(g_table[s].name, full) == 0) return s;
    pthread_mutex_lock(&g_lock);
    for (s = 1; s < g_ntables; s++) if (strcmp(g_table[s].name, full) == 0) break;
    if (s >= g_ntables) {
        if (g_ntables == 0) { snprintf(g_table[0].name, sizeof g_table[0].name, "(overflow)"); g_ntables = 1; s = 1; }
        if (g_ntables < MAX_TABLES) { s = g_ntables; snprintf(g_table[s].name, sizeof g_table[s].name, "%s", full); g_ntables++; }
        else s = 0;
    }
    __atomic_store_n(&g_env[e].slot[dbi], (int16_t)s, __ATOMIC_RELEASE);
    pthread_mutex_unlock(&g_lock);
    return s;
}

static void record(int slot, int call, int rc, size_t kbytes, size_t vbytes, uint64_t ns) {
    Cell *c = &g_table[slot].cell[call];
    ADD(c->count, 1);
    if (rc == MDB_NOTFOUND) ADD(c->miss, 1);
    ADD(c->key_bytes, kbytes);
    ADD(c->value_bytes, vbytes);
    ADD(c->ns, ns);
}

static __attribute__((noinline)) int kvstats_dbi_open(MDB_txn *txn, const char *name, unsigned int flags, MDB_dbi *dbi) {
    READY();
    t_depth++;
    int rc = r_dbi_open(txn, name, flags, dbi);
    t_depth--;
    if (rc == 0 && dbi) table_slot(r_txn_env(txn), *dbi, name, 1);
    return rc;
}

static __attribute__((noinline)) int kvstats_get(MDB_txn *txn, MDB_dbi dbi, MDB_val *key, MDB_val *data) {
    READY();
    if (t_depth) return r_get(txn, dbi, key, data);
    t_depth++;
    uint64_t t0 = now_ns();
    int rc = r_get(txn, dbi, key, data);
    uint64_t ns = now_ns() - t0;
    t_depth--;
    record(table_slot(r_txn_env(txn), dbi, NULL, 0), C_GET, rc, key ? key->mv_size : 0,
           rc == 0 && data ? data->mv_size : 0, ns);
    return rc;
}

static __attribute__((noinline)) int kvstats_cursor_get(MDB_cursor *cur, MDB_val *key, MDB_val *data, int op) {
    READY();
    if (t_depth) return r_cursor_get(cur, key, data, op);
    int call = op == MDB_SET_KEY || op == MDB_SET ? C_CUR_SET_KEY : op == MDB_SET_RANGE ? C_CUR_SET_RANGE
             : op == MDB_FIRST ? C_CUR_FIRST : op == MDB_LAST ? C_CUR_LAST : op == MDB_NEXT ? C_CUR_NEXT
             : op == MDB_PREV ? C_CUR_PREV : op == MDB_GET_CURRENT ? C_CUR_GET_CURRENT : C_CUR_OTHER;
    size_t probe = key && (call == C_CUR_SET_KEY || call == C_CUR_SET_RANGE) ? key->mv_size : 0;
    t_depth++;
    uint64_t t0 = now_ns();
    int rc = r_cursor_get(cur, key, data, op);
    uint64_t ns = now_ns() - t0;
    t_depth--;
    record(table_slot(r_txn_env(r_cursor_txn(cur)), r_cursor_dbi(cur), NULL, 0), call, rc,
           rc == 0 && key ? key->mv_size : probe, rc == 0 && data ? data->mv_size : 0, ns);
    return rc;
}

static __attribute__((noinline)) int kvstats_put(MDB_txn *txn, MDB_dbi dbi, MDB_val *key, MDB_val *data, unsigned int flags) {
    READY();
    if (t_depth) return r_put(txn, dbi, key, data, flags);
    size_t k = key ? key->mv_size : 0, v = data ? data->mv_size : 0;
    t_depth++;
    uint64_t t0 = now_ns();
    int rc = r_put(txn, dbi, key, data, flags);
    uint64_t ns = now_ns() - t0;
    t_depth--;
    record(table_slot(r_txn_env(txn), dbi, NULL, 0), C_PUT, rc, k, v, ns);
    return rc;
}

static __attribute__((noinline)) int kvstats_cursor_put(MDB_cursor *cur, MDB_val *key, MDB_val *data, unsigned int flags) {
    READY();
    if (t_depth) return r_cursor_put(cur, key, data, flags);
    size_t k = key ? key->mv_size : 0, v = data ? data->mv_size : 0;
    t_depth++;
    uint64_t t0 = now_ns();
    int rc = r_cursor_put(cur, key, data, flags);
    uint64_t ns = now_ns() - t0;
    t_depth--;
    record(table_slot(r_txn_env(r_cursor_txn(cur)), r_cursor_dbi(cur), NULL, 0), C_CUR_PUT, rc, k, v, ns);
    return rc;
}

static __attribute__((noinline)) int kvstats_del(MDB_txn *txn, MDB_dbi dbi, MDB_val *key, MDB_val *data) {
    READY();
    if (t_depth) return r_del(txn, dbi, key, data);
    size_t k = key ? key->mv_size : 0;
    t_depth++;
    uint64_t t0 = now_ns();
    int rc = r_del(txn, dbi, key, data);
    uint64_t ns = now_ns() - t0;
    t_depth--;
    record(table_slot(r_txn_env(txn), dbi, NULL, 0), C_DEL, rc, k, 0, ns);
    return rc;
}

static __attribute__((noinline)) int kvstats_cursor_del(MDB_cursor *cur, unsigned int flags) {
    READY();
    if (t_depth) return r_cursor_del(cur, flags);
    int slot = table_slot(r_txn_env(r_cursor_txn(cur)), r_cursor_dbi(cur), NULL, 0);
    t_depth++;
    uint64_t t0 = now_ns();
    int rc = r_cursor_del(cur, flags);
    uint64_t ns = now_ns() - t0;
    t_depth--;
    record(slot, C_CUR_DEL, rc, 0, 0, ns);   /* the cursor's key is not passed to this call */
    return rc;
}

static __attribute__((noinline)) int kvstats_txn_commit(MDB_txn *txn) {
    READY();
    if (t_depth) return r_txn_commit(txn);
    t_depth++;
    uint64_t t0 = now_ns();
    int rc = r_txn_commit(txn);
    uint64_t ns = now_ns() - t0;
    t_depth--;
    ADD(g_commit_count, 1);
    ADD(g_commit_ns, ns);
    return rc;
}

/* What the dynamic linker binds TuGraph's LMDB calls to. Each only jumps to its kvstats_ function, so a profiler
 * sees the counting (the kvstats_ frames: ours, not the store's) apart from LMDB's own mdb_ functions below it. */
int mdb_dbi_open(MDB_txn *txn, const char *name, unsigned int flags, MDB_dbi *dbi) { return kvstats_dbi_open(txn, name, flags, dbi); }
int mdb_get(MDB_txn *txn, MDB_dbi dbi, MDB_val *key, MDB_val *data) { return kvstats_get(txn, dbi, key, data); }
int mdb_cursor_get(MDB_cursor *cur, MDB_val *key, MDB_val *data, int op) { return kvstats_cursor_get(cur, key, data, op); }
int mdb_put(MDB_txn *txn, MDB_dbi dbi, MDB_val *key, MDB_val *data, unsigned int flags) { return kvstats_put(txn, dbi, key, data, flags); }
int mdb_cursor_put(MDB_cursor *cur, MDB_val *key, MDB_val *data, unsigned int flags) { return kvstats_cursor_put(cur, key, data, flags); }
int mdb_del(MDB_txn *txn, MDB_dbi dbi, MDB_val *key, MDB_val *data) { return kvstats_del(txn, dbi, key, data); }
int mdb_cursor_del(MDB_cursor *cur, unsigned int flags) { return kvstats_cursor_del(cur, flags); }
int mdb_txn_commit(MDB_txn *txn) { return kvstats_txn_commit(txn); }

/* ---- the listener ---- */

static void reset_all(void) {
    for (int s = 0; s < MAX_TABLES; s++)
        for (int c = 0; c < N_CALLS; c++) {
            Cell *x = &g_table[s].cell[c];
            __atomic_store_n(&x->count, 0, __ATOMIC_RELAXED); __atomic_store_n(&x->miss, 0, __ATOMIC_RELAXED);
            __atomic_store_n(&x->key_bytes, 0, __ATOMIC_RELAXED); __atomic_store_n(&x->value_bytes, 0, __ATOMIC_RELAXED);
            __atomic_store_n(&x->ns, 0, __ATOMIC_RELAXED);
        }
    __atomic_store_n(&g_commit_count, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&g_commit_ns, 0, __ATOMIC_RELAXED);
}

static Cell load(const Cell *x) {
    Cell c = {__atomic_load_n(&x->count, __ATOMIC_RELAXED), __atomic_load_n(&x->miss, __ATOMIC_RELAXED),
              __atomic_load_n(&x->key_bytes, __ATOMIC_RELAXED), __atomic_load_n(&x->value_bytes, __ATOMIC_RELAXED),
              __atomic_load_n(&x->ns, __ATOMIC_RELAXED)};
    return c;
}

static char *dump_json(size_t *len) {
    size_t cap = 1 << 16, n = 0;
    char *b = malloc(cap);
#define P(...) do { if (cap - n < 1024) b = realloc(b, cap *= 2); n += (size_t)snprintf(b + n, cap - n, __VA_ARGS__); } while (0)
    int nt = g_ntables;
    Cell by_call[N_CALLS] = {{0}}, by_op[N_OPS] = {{0}};
    for (int s = 0; s < nt; s++)
        for (int c = 0; c < N_CALLS; c++) {
            Cell x = load(&g_table[s].cell[c]);
            Cell *t[2] = {&by_call[c], &by_op[CALL_OP[c]]};
            for (int i = 0; i < 2; i++) {
                t[i]->count += x.count; t[i]->miss += x.miss; t[i]->key_bytes += x.key_bytes;
                t[i]->value_bytes += x.value_bytes; t[i]->ns += x.ns;
            }
        }
    P("{");
    for (int o = 0; o < N_OPS; o++) {
        const Cell *x = &by_op[o];
        P("\"%s_count\":%llu,\"%s_miss_count\":%llu,\"%s_key_bytes\":%llu,\"%s_value_bytes\":%llu,\"%s_bytes\":%llu,"
          "\"%s_latency_ns\":%llu,", OP_NAME[o], (unsigned long long)x->count, OP_NAME[o], (unsigned long long)x->miss,
          OP_NAME[o], (unsigned long long)x->key_bytes, OP_NAME[o], (unsigned long long)x->value_bytes, OP_NAME[o],
          (unsigned long long)(x->key_bytes + x->value_bytes), OP_NAME[o], (unsigned long long)x->ns);
    }
    P("\"commit_count\":%llu,\"commit_latency_ns\":%llu,\"by_call\":{",
      (unsigned long long)__atomic_load_n(&g_commit_count, __ATOMIC_RELAXED),
      (unsigned long long)__atomic_load_n(&g_commit_ns, __ATOMIC_RELAXED));
    for (int c = 0; c < N_CALLS; c++) {
        const Cell *x = &by_call[c];
        P("%s\"%s\":{\"count\":%llu,\"miss_count\":%llu,\"key_bytes\":%llu,\"value_bytes\":%llu,\"latency_ns\":%llu}",
          c ? "," : "", CALL_NAME[c], (unsigned long long)x->count, (unsigned long long)x->miss,
          (unsigned long long)x->key_bytes, (unsigned long long)x->value_bytes, (unsigned long long)x->ns);
    }
    P("},\"by_table\":{");
    int first = 1;
    for (int s = 0; s < nt; s++) {
        int any = 0;
        for (int c = 0; c < N_CALLS; c++) any |= load(&g_table[s].cell[c]).count != 0;
        if (!any) continue;
        P("%s\"", first ? "" : ",");
        for (const char *p = g_table[s].name; *p; p++) {   /* a table's name is TuGraph's; escape it for JSON */
            if (*p == '"' || *p == '\\') P("\\%c", *p);
            else if ((unsigned char)*p < 0x20) P("\\u%04x", *p);
            else P("%c", *p);
        }
        P("\":{");
        first = 0;
        int f2 = 1;
        for (int c = 0; c < N_CALLS; c++) {
            Cell x = load(&g_table[s].cell[c]);
            if (!x.count) continue;
            P("%s\"%s\":{\"count\":%llu,\"miss_count\":%llu,\"key_bytes\":%llu,\"value_bytes\":%llu,\"latency_ns\":%llu}",
              f2 ? "" : ",", CALL_NAME[c], (unsigned long long)x.count, (unsigned long long)x.miss,
              (unsigned long long)x.key_bytes, (unsigned long long)x.value_bytes, (unsigned long long)x.ns);
            f2 = 0;
        }
        P("}");
    }
    P("}}\n");
#undef P
    *len = n;
    return b;
}

static void reply(int fd, const char *status, const char *body, size_t len) {
    char head[160];
    int h = snprintf(head, sizeof head, "HTTP/1.1 %s\r\nContent-Type: application/json\r\nContent-Length: %zu\r\n"
                     "Connection: close\r\n\r\n", status, len);
    if (write(fd, head, (size_t)h) < 0 || write(fd, body, len) < 0) { /* the reader went away */ }
}

static void *serve(void *arg) {
    int srv = (int)(intptr_t)arg;
    for (;;) {
        int fd = accept(srv, NULL, NULL);
        if (fd < 0) continue;
        char req[2048];
        ssize_t n = read(fd, req, sizeof req - 1);
        if (n > 0) {
            req[n] = 0;
            if (!strncmp(req, "GET /kvstats ", 13)) {
                size_t len;
                char *body = dump_json(&len);
                reply(fd, "200 OK", body, len);
                free(body);
            } else if (!strncmp(req, "PUT /kvstats/reset ", 19) || !strncmp(req, "POST /kvstats/reset ", 20)) {
                reset_all();
                reply(fd, "200 OK", "{\"reset\":true}\n", 15);
            } else {
                reply(fd, "404 Not Found", "{\"error\":\"GET /kvstats or PUT /kvstats/reset\"}\n", 47);
            }
        }
        close(fd);
    }
    return NULL;
}

__attribute__((constructor)) static void start_listener(void) {
    const char *port = getenv("TUGRAPH_KVSTATS_PORT");
    if (!port || !*port) return;
    int srv = socket(AF_INET, SOCK_STREAM, 0), one = 1;
    struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons((uint16_t)atoi(port)),
                            .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    if (srv < 0 || bind(srv, (struct sockaddr *)&a, sizeof a) < 0 || listen(srv, 16) < 0) {
        fprintf(stderr, "tugraph_kvstats: cannot listen on 127.0.0.1:%s; the counters run with no listener\n", port);
        if (srv >= 0) close(srv);
        return;
    }
    pthread_t t;
    if (pthread_create(&t, NULL, serve, (void *)(intptr_t)srv) == 0) pthread_detach(t);
    fprintf(stderr, "tugraph_kvstats: counting LMDB calls; GET http://127.0.0.1:%s/kvstats\n", port);
}
