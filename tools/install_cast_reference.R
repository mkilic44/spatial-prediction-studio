# Installs the CRAN packages of reference-r-packages.csv into reference-r/library-extra from a folder
# of Windows binaries whose SHA-256 digests the caller has already checked (installer/sps_install.py
# or tools/cast_reference.py). Nothing is downloaded here.
# Usage: Rscript --vanilla install_cast_reference.R <binaries folder>
# Run with the private Rscript and the folder that contains reference-r as working directory.
args <- commandArgs(trailingOnly=TRUE)
stopifnot(length(args) == 1)
binaries <- normalizePath(args[1], winslash="/", mustWork=TRUE)
lib <- normalizePath("reference-r", winslash="/")
lib <- paste0(lib, "/library-extra")
dir.create(lib, recursive=TRUE, showWarnings=FALSE)
.libPaths(c(lib,.libPaths()))
plan <- read.csv("reference-r-packages.csv", stringsAsFactors=FALSE)
files <- file.path(binaries, paste0(plan$Package, "_", plan$Version, ".zip"))
stopifnot(all(file.exists(files)))
install.packages(files, lib=lib, repos=NULL, type="win.binary")
library(CAST)
stopifnot(as.character(packageVersion("CAST"))=="1.0.4")
actual <- do.call(rbind,lapply(plan$Package,function(package) {
    folder <- normalizePath(file.path(lib,package),winslash='/',mustWork=TRUE)
    stopifnot(tolower(normalizePath(find.package(package),winslash='/'))==tolower(folder))
    read.dcf(file.path(folder,'DESCRIPTION'),fields=c('Package','Version','License'))
}))
stopifnot(all(actual[,'Package']==plan$Package),all(actual[,'Version']==plan$Version))
dir.create('build',showWarnings=FALSE)
write.csv(actual,"build/reference-cast-installed.csv",row.names=FALSE)
writeLines(capture.output(sessionInfo()),"build/reference-cast-session.txt")
cat("CAST 1.0.4 loaded successfully.\n")
