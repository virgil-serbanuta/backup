load("@rules_cc//cc:defs.bzl", "cc_library")

package(default_visibility = ["//visibility:public"])

cc_library(
    name = "change-listener",
    srcs = ["change-listener.cpp"],
    hdrs = ["change-listener.h"],
    deps = [],
)

cc_library(
    name = "change-notifier",
    srcs = ["change-notifier.cpp"],
    hdrs = ["change-notifier.h"],
    deps = [
        ":change-listener",
    ],
)

cc_library(
    name = "thing-with-error",
    srcs = ["thing-with-error.cpp"],
    hdrs = ["thing-with-error.h"],
    deps = [],
)
