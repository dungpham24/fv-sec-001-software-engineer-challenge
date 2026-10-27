/*
 * FV-SEC001 Native C99 / POSIX Accelerator (pread + pthread + FNV-1a Open-Addressing Hash Table)
 * Zero external dependencies — uses only standard C99 and POSIX headers.
 */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include <pthread.h>

#define TABLE_SIZE 1024
#define READ_BUF_SIZE (512 * 1024)

typedef struct {
    char cid[64];
    uint32_t cid_len;
    int64_t impressions;
    int64_t clicks;
    int64_t spend_scaled;
    int64_t conversions;
    int used;
} Bucket;

typedef struct {
    int fd;
    size_t start;
    size_t end;
    Bucket table[TABLE_SIZE];
    int64_t valid_rows;
    int64_t malformed_rows;
} WorkerArgs;

static inline uint32_t hash_bytes(const uint8_t *s, size_t len) {
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < len; i++) {
        h ^= s[i];
        h *= 16777619u;
    }
    return h;
}

static inline int is_ws(uint8_t c) {
    return (c == ' ' || c == '\t' || c == '\r' || c == '\v' || c == '\f');
}

static inline void strip_bom(const uint8_t **ptr, size_t *len) {
    const uint8_t *p = *ptr;
    size_t l = *len;
    if (l >= 3 && p[0] == 0xEF && p[1] == 0xBB && p[2] == 0xBF) {
        *ptr = p + 3;
        *len = l - 3;
    } else if (l >= 6 && p[0] == 0xC3 && p[1] == 0xAF && p[2] == 0xC2 &&
               p[3] == 0xBB && p[4] == 0xC2 && p[5] == 0xBF) {
        *ptr = p + 6;
        *len = l - 6;
    }
}

static inline void trim_slice(const uint8_t **ptr, size_t *len) {
    const uint8_t *p = *ptr;
    size_t l = *len;
    while (l > 0 && is_ws(*p)) {
        p++;
        l--;
    }
    while (l > 0 && is_ws(p[l - 1])) {
        l--;
    }
    *ptr = p;
    *len = l;
}

static inline int parse_nonneg_int64(const uint8_t *ptr, size_t len, int64_t *out) {
    if (len == 0) {
        return 0;
    }
    if (*ptr == '+') {
        ptr++;
        len--;
        if (len == 0) {
            return 0;
        }
    }
    int64_t val = 0;
    for (size_t i = 0; i < len; i++) {
        uint8_t c = ptr[i];
        if (c < '0' || c > '9') {
            return 0;
        }
        val = val * 10 + (c - '0');
    }
    *out = val;
    return 1;
}

static inline int parse_spend_scaled(const uint8_t *ptr, size_t len, int64_t *out) {
    if (len == 0) {
        return 0;
    }
    if (*ptr == '+') {
        ptr++;
        len--;
        if (len == 0) {
            return 0;
        }
    }
    int64_t spend_int = 0;
    int64_t spend_frac = 0;
    int frac_scale = 10000;
    int round_digit = 0;
    int in_frac = 0;
    int has_digits = 0;

    for (size_t i = 0; i < len; i++) {
        uint8_t c = ptr[i];
        if (c >= '0' && c <= '9') {
            has_digits = 1;
            if (!in_frac) {
                spend_int = spend_int * 10 + (c - '0');
            } else if (frac_scale > 1) {
                frac_scale /= 10;
                spend_frac += (c - '0') * frac_scale;
            } else if (frac_scale == 1) {
                round_digit = (c - '0');
                frac_scale = 0;
            }
        } else if (c == '.' && !in_frac) {
            in_frac = 1;
        } else {
            return 0;
        }
    }

    if (!has_digits) {
        return 0;
    }

    *out = spend_int * 10000 + spend_frac + (round_digit >= 5 ? 1 : 0);
    return 1;
}

static inline void process_line(WorkerArgs *w, const uint8_t *line_ptr, size_t len) {
    while (len > 0 && line_ptr[len - 1] == '\r') {
        len--;
    }

    strip_bom(&line_ptr, &len);

    int has_non_ws = 0;
    for (size_t i = 0; i < len; i++) {
        if (!is_ws(line_ptr[i])) {
            has_non_ws = 1;
            break;
        }
    }
    if (!has_non_ws) {
        return;
    }

    size_t first_comma = 0;
    while (first_comma < len && line_ptr[first_comma] != ',') {
        first_comma++;
    }

    const uint8_t *col0 = line_ptr;
    size_t col0_len = first_comma;
    trim_slice(&col0, &col0_len);
    strip_bom(&col0, &col0_len);
    trim_slice(&col0, &col0_len);
    if (col0_len == 11 && strncasecmp((const char *)col0, "campaign_id", 11) == 0) {
        return;
    }

    size_t commas[6];
    int n_commas = 0;
    for (size_t i = 0; i < len; i++) {
        if (line_ptr[i] == ',') {
            if (n_commas < 6) {
                commas[n_commas] = i;
            }
            n_commas++;
        }
    }

    if (n_commas != 5) {
        w->malformed_rows++;
        return;
    }

    const uint8_t *cid = col0;
    size_t cid_len = col0_len;

    const uint8_t *date_p = line_ptr + commas[0] + 1;
    size_t date_len = commas[1] - (commas[0] + 1);
    trim_slice(&date_p, &date_len);

    if (cid_len == 0 || cid_len >= 63 || date_len == 0) {
        w->malformed_rows++;
        return;
    }

    const uint8_t *imp_p = line_ptr + commas[1] + 1;
    size_t imp_len = commas[2] - (commas[1] + 1);
    trim_slice(&imp_p, &imp_len);

    const uint8_t *clk_p = line_ptr + commas[2] + 1;
    size_t clk_len = commas[3] - (commas[2] + 1);
    trim_slice(&clk_p, &clk_len);

    const uint8_t *spend_p = line_ptr + commas[3] + 1;
    size_t spend_len = commas[4] - (commas[3] + 1);
    trim_slice(&spend_p, &spend_len);

    const uint8_t *conv_p = line_ptr + commas[4] + 1;
    size_t conv_len = len - (commas[4] + 1);
    trim_slice(&conv_p, &conv_len);

    int64_t imp = 0, clk = 0, conv = 0, spend_scaled = 0;
    if (!parse_nonneg_int64(imp_p, imp_len, &imp) ||
        !parse_nonneg_int64(clk_p, clk_len, &clk) ||
        !parse_nonneg_int64(conv_p, conv_len, &conv) ||
        clk > imp ||
        !parse_spend_scaled(spend_p, spend_len, &spend_scaled)) {
        w->malformed_rows++;
        return;
    }

    w->valid_rows++;

    uint32_t idx = hash_bytes(cid, cid_len) & (TABLE_SIZE - 1);
    while (1) {
        Bucket *b = &w->table[idx];
        if (!b->used) {
            b->used = 1;
            b->cid_len = (uint32_t)cid_len;
            memcpy(b->cid, cid, cid_len);
            b->cid[cid_len] = '\0';
            b->impressions = imp;
            b->clicks = clk;
            b->spend_scaled = spend_scaled;
            b->conversions = conv;
            break;
        }
        if (b->cid_len == cid_len && memcmp(b->cid, cid, cid_len) == 0) {
            b->impressions += imp;
            b->clicks += clk;
            b->spend_scaled += spend_scaled;
            b->conversions += conv;
            break;
        }
        idx = (idx + 1) & (TABLE_SIZE - 1);
    }
}

static void *worker_fn(void *arg) {
    WorkerArgs *w = (WorkerArgs *)arg;
    uint8_t *buf = (uint8_t *)malloc(READ_BUF_SIZE + 65536);
    if (!buf) {
        return NULL;
    }

    size_t offset = w->start;
    size_t carry = 0;

    while (offset < w->end) {
        size_t to_read = w->end - offset;
        if (to_read > READ_BUF_SIZE) {
            to_read = READ_BUF_SIZE;
        }
        ssize_t n = pread(w->fd, buf + carry, to_read, (off_t)offset);
        if (n <= 0) {
            break;
        }
        offset += (size_t)n;
        size_t total = carry + (size_t)n;

        size_t line_start = 0;
        const uint8_t *p = buf;
        const uint8_t *end_p = buf + total;
        while (p < end_p) {
            const uint8_t *nl = (const uint8_t *)memchr(p, '\n', (size_t)(end_p - p));
            if (!nl) {
                break;
            }
            size_t line_idx = (size_t)(nl - buf);
            process_line(w, buf + line_start, line_idx - line_start);
            line_start = line_idx + 1;
            p = nl + 1;
        }

        carry = total - line_start;
        if (carry > 0 && line_start > 0) {
            memmove(buf, buf + line_start, carry);
        }
    }

    if (carry > 0) {
        process_line(w, buf, carry);
    }

    free(buf);
    return NULL;
}

int process_all_parallel(
    int fd,
    const size_t *starts,
    const size_t *ends,
    int n_chunks,
    WorkerArgs *workers
) {
    if (n_chunks <= 0) {
        return 0;
    }

    for (int i = 0; i < n_chunks; i++) {
        workers[i].fd = fd;
        workers[i].start = starts[i];
        workers[i].end = ends[i];
        workers[i].valid_rows = 0;
        workers[i].malformed_rows = 0;
        memset(workers[i].table, 0, sizeof(workers[i].table));
    }

    if (n_chunks == 1) {
        worker_fn(&workers[0]);
        return 0;
    }

    pthread_t *threads = (pthread_t *)malloc(sizeof(pthread_t) * (size_t)(n_chunks - 1));
    if (!threads) {
        return -1;
    }

    for (int i = 1; i < n_chunks; i++) {
        pthread_create(&threads[i - 1], NULL, worker_fn, &workers[i]);
    }

    /* Run chunk 0 on the calling thread to avoid one thread spawn */
    worker_fn(&workers[0]);

    for (int i = 1; i < n_chunks; i++) {
        pthread_join(threads[i - 1], NULL);
    }

    free(threads);
    return 0;
}
