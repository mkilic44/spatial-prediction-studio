# Lists CAST and its recursive dependencies (Depends, Imports, LinkingTo) as CRAN offers them for
# R 4.4 on Windows. CRAN no longer rebuilds R 4.4 binaries, so this list stays fixed; the MD5 sums
# let tools/install_cast_reference.R check every downloaded file.
options(repos=c(CRAN="https://cloud.r-project.org"), timeout=300)
stopifnot(as.character(getRversion()) == "4.4.3")
available <- available.packages(type="win.binary")
stopifnot("CAST" %in% rownames(available), available["CAST", "Version"] == "1.0.4")
packages <- unique(c("CAST", tools::package_dependencies("CAST", db=available,
    which=c("Depends","Imports","LinkingTo"), recursive=TRUE)[["CAST"]]))
packages <- intersect(packages, rownames(available))
stopifnot(all(!is.na(available[packages, "MD5sum"])))
dir.create("build", showWarnings=FALSE)
write.csv(available[packages, c("Package","Version","License","Repository","MD5sum")],
          "build/reference-cast-packages-plan.csv", row.names=FALSE)
cat("R version:", R.version.string, "\n")
cat("CAST version:", available["CAST","Version"], "\n")
cat("Available dependency count:", length(packages), "\n")
