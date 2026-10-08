idf_component_register(SRCS "wy_mww.c" "wy_mww_scorer.cpp" INCLUDE_DIRS "."
    PRIV_REQUIRES esp-tflite-micro esp-micro-speech-features heap log)
