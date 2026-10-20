.libPaths(c(normalizePath("reference-r/library-extra",winslash="/"),.libPaths()))
library(CAST)
dir.create("build/reference-cast-source",showWarnings=FALSE)
ns <- asNamespace("CAST")
writeLines(ls(ns,all.names=TRUE),"build/reference-cast-source/functions.txt")
for (name in c("knndm","trainDI","aoa","aoa.default","aoa.data.frame", "knndm_geo", "check_knndm_geo", "user_weights", "aoa_categorial_train", ".alldistfun")) {
 if(exists(name,ns,inherits=FALSE))
   writeLines(capture.output(print(get(name,ns))),paste0("build/reference-cast-source/",name,".R"))
}
cat("Read installed CAST",as.character(packageVersion("CAST")),"functions\n")
