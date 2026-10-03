#include "wyoming_json.h"
#include "cJSON.h"
#include <string.h>
#include <math.h>
#include <stdlib.h>

static int digit(char c) { return c >= '0' && c <= '9'; }

/* cJSON accepts some non-JSON number spellings. Check token grammar first.
 * Disallow decoded NUL in strings because cJSON represents them as C strings.
 */
static int lexical_check(const char *s, size_t n) {
    int quoted = 0;
    for (size_t i = 0; i < n; ++i) {
        unsigned char c = (unsigned char)s[i];
        if (!c) return WY_INVALID;
        if (quoted) {
            if (c < 0x20) return WY_INVALID;
            if (c == '"') quoted = 0;
            else if (c == '\\') {
                if (++i >= n) return WY_INVALID;
                if (s[i] == 'u' && n - i >= 5 &&
                    memcmp(s + i, "u0000", 5) == 0) return WY_INVALID;
            }
            continue;
        }
        if (c == '"') { quoted = 1; continue; }
        if (c < 0x20 && c != '\t' && c != '\n' && c != '\r') return WY_INVALID;
        if (c != '-' && !digit((char)c)) continue;
        size_t p = i;
        if (s[p] == '-') ++p;
        if (p >= n || !digit(s[p])) return WY_INVALID;
        if (s[p] == '0') ++p;
        else while (p < n && digit(s[p])) ++p;
        if (p < n && s[p] == '.') {
            ++p;
            if (p >= n || !digit(s[p])) return WY_INVALID;
            while (p < n && digit(s[p])) ++p;
        }
        if (p < n && (s[p] == 'e' || s[p] == 'E')) {
            ++p;
            if (p < n && (s[p] == '+' || s[p] == '-')) ++p;
            if (p >= n || !digit(s[p])) return WY_INVALID;
            while (p < n && digit(s[p])) ++p;
        }
        if (p < n && s[p] != ',' && s[p] != '}' && s[p] != ']' &&
            s[p] != ' ' && s[p] != '\t' && s[p] != '\r' && s[p] != '\n')
            return WY_INVALID;
        i = p - 1;
    }
    return quoted ? WY_INVALID : WY_OK;
}

static int read_length(const cJSON *item, size_t maximum, size_t *out) {
    *out = 0;
    if (!item) return WY_OK;
    if (!cJSON_IsNumber(item) || !isfinite(item->valuedouble) ||
        item->valuedouble < 0 || floor(item->valuedouble) != item->valuedouble)
        return WY_INVALID;
    if (item->valuedouble > (double)maximum) return WY_LIMIT;
    *out = (size_t)item->valuedouble;
    return WY_OK;
}

int wy_parse_header(const char *header, size_t length, wy_frame *frame) {
    if (!header || !frame || !length) return WY_INVALID;
    memset(frame, 0, sizeof(*frame));
    if (length >= WY_HEADER_MAX) return WY_LIMIT;
    if (lexical_check(header, length) != WY_OK) return WY_INVALID;
    char bounded[WY_HEADER_MAX];
    memcpy(bounded, header, length);
    bounded[length] = '\0';
    cJSON *root = cJSON_ParseWithLengthOpts(bounded, length + 1, NULL, 1);
    if (!root) return WY_INVALID;
    int result = WY_INVALID;
    wy_frame candidate = {0};
    if (!cJSON_IsObject(root)) goto done;
    for (const cJSON *a = root->child; a; a = a->next) {
        for (const cJSON *b = a->next; b; b = b->next) {
            if (strcmp(a->string, b->string) == 0) goto done;
        }
    }
    const cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
    const cJSON *inline_data = cJSON_GetObjectItemCaseSensitive(root, "data");
    if (inline_data && !cJSON_IsObject(inline_data)) goto done;
    if (!cJSON_IsString(type)) goto done;
    size_t count = strlen(type->valuestring);
    if (!count || count > 96) goto done;
    for (size_t i = 0; i < count; ++i) {
        char c = type->valuestring[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              digit(c) || c == '-' || c == '.' || c == '_')) goto done;
    }
    memcpy(candidate.type, type->valuestring, count + 1);
    result = read_length(cJSON_GetObjectItemCaseSensitive(root, "data_length"),
                         WY_DATA_MAX, &candidate.data_length);
    if (result != WY_OK) goto done;
    result = read_length(cJSON_GetObjectItemCaseSensitive(root, "payload_length"),
                         WY_PAYLOAD_MAX, &candidate.payload_length);
    if (result == WY_OK) *frame = candidate;
done:
    cJSON_Delete(root);
    return result;
}

static int utf8_valid(const char *text, size_t length) {
    const unsigned char *s = (const unsigned char *)text;
    for (size_t i = 0; i < length; ++i) {
        unsigned char lead = s[i];
        if (lead < 0x80) continue;
        size_t following;
        if (lead >= 0xc2 && lead <= 0xdf) following = 1;
        else if (lead >= 0xe0 && lead <= 0xef) following = 2;
        else if (lead >= 0xf0 && lead <= 0xf4) following = 3;
        else return 0;
        if (following >= length - i) return 0;
        unsigned char second = s[i + 1];
        if ((lead == 0xe0 && second < 0xa0) || (lead == 0xed && second > 0x9f) ||
            (lead == 0xf0 && second < 0x90) || (lead == 0xf4 && second > 0x8f)) return 0;
        for (size_t j = 1; j <= following; ++j)
            if (s[i + j] < 0x80 || s[i + j] > 0xbf) return 0;
        i += following;
    }
    return 1;
}

static int unique_finite_tree(const cJSON *item) {
    if (cJSON_IsNumber(item) && !isfinite(item->valuedouble)) return 0;
    if (cJSON_IsObject(item)) {
        for (const cJSON *a = item->child; a; a = a->next)
            for (const cJSON *b = a->next; b; b = b->next)
                if (!strcmp(a->string, b->string)) return 0;
    }
    for (const cJSON *child = item->child; child; child = child->next)
        if (!unique_finite_tree(child)) return 0;
    return 1;
}

static cJSON *strict_object(const char *text, size_t length) {
    if (!text || !length || !utf8_valid(text, length) || lexical_check(text, length)) return NULL;
    char *bounded = malloc(length + 1);
    if (!bounded) return NULL;
    memcpy(bounded, text, length);
    bounded[length] = 0;
    cJSON *root = cJSON_ParseWithLengthOpts(bounded, length + 1, NULL, 1);
    free(bounded);
    if (root && (!cJSON_IsObject(root) || !unique_finite_tree(root))) {
        cJSON_Delete(root);
        root = NULL;
    }
    return root;
}

int wy_decode_control(const char *header, size_t header_length,
                      const char *body, size_t body_length, cJSON **data) {
    if (!data) return WY_INVALID;
    *data = NULL;
    wy_frame frame;
    int result = wy_parse_header(header, header_length, &frame);
    if (result) return result;
    if (frame.payload_length || frame.data_length != body_length) return WY_INVALID;
    cJSON *root = strict_object(header, header_length);
    if (!root) return WY_INVALID;
    cJSON *merged = cJSON_DetachItemFromObjectCaseSensitive(root, "data");
    cJSON_Delete(root);
    if (!merged) merged = cJSON_CreateObject();
    if (!merged) return WY_IO;
    if (body_length) {
        root = strict_object(body, body_length);
        if (!root) { cJSON_Delete(merged); return WY_INVALID; }
        for (const cJSON *item = root->child; item; item = item->next) {
            cJSON *copy = cJSON_Duplicate(item, 1);
            if (!copy) { result = WY_IO; break; }
            cJSON_DeleteItemFromObjectCaseSensitive(merged, item->string);
            if (!cJSON_AddItemToObject(merged, item->string, copy)) {
                cJSON_Delete(copy);
                result = WY_IO;
                break;
            }
        }
        cJSON_Delete(root);
    }
    if (result) cJSON_Delete(merged);
    else *data = merged;
    return result;
}
