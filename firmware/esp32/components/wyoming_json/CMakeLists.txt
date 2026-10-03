idf_component_register(SRCS "wyoming_json.c" "vendor/cjson/cJSON.c"
    INCLUDE_DIRS "." "vendor/cjson" REQUIRES wyoming_wire)
target_compile_definitions(${COMPONENT_LIB} PRIVATE CJSON_NESTING_LIMIT=16)
