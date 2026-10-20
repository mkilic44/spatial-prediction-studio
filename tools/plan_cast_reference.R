options(repos=c(CRAN="https://cloud.r-project.org"), timeout=300)
available <- available.packages(type="win.binary")
stopifnot("CAST" %in% rownames(available))
packages <- unique(c("CAST", tools::package_dependencies("CAST", db=available,
    which=c("Depends","Imports","LinkingTo"), recursive=TRUE)[["CAST"]]))
packages <- intersect(packages, rownames(available))
write.csv(available[packages,c("Package","Version","License","Repository")],
          "build/reference-cast-packages-plan.csv", row.names=FALSE)
cat("R version:", R.version.string, "\n")
cat("CAST version:", available["CAST","Version"], "\n")
cat("Available dependency count:", length(packages), "\n")
