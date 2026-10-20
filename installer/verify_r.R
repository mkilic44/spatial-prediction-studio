args <- commandArgs(trailingOnly=TRUE)
root <- normalizePath(args[1], winslash="/", mustWork=TRUE)
lib <- normalizePath(file.path(root, "reference-r", "library-extra"), winslash="/", mustWork=TRUE)
.libPaths(c(lib, .libPaths()))
stopifnot(as.character(getRversion()) == "4.4.3")
expected <- read.csv(file.path(root, "reference-r-packages.csv"), stringsAsFactors=FALSE)
for (i in seq_len(nrow(expected))) {
  pkg <- expected$Package[i]
  found <- normalizePath(find.package(pkg), winslash="/", mustWork=TRUE)
  stopifnot(startsWith(tolower(found), tolower(paste0(root, "/reference-r/"))))
  version <- read.dcf(file.path(found, "DESCRIPTION"), fields="Version")[[1]]
  stopifnot(version == expected$Version[i])
}
suppressPackageStartupMessages(library(CAST))
stopifnot(as.character(packageVersion("CAST")) == "1.0.4")
cat("SPS_REFERENCE_R_OK\n")
cat("R=", as.character(getRversion()), "; CAST=", as.character(packageVersion("CAST")), "; locked_packages=", nrow(expected), "\n", sep="")
flush.console()
# Give the owning verification process a bounded readback window for DLL paths.
Sys.sleep(1)
