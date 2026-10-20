args <- commandArgs(trailingOnly=TRUE)
stopifnot(length(args)==7)
.libPaths(c(args[7],.libPaths()))
library(CAST)
stopifnot(as.character(packageVersion('CAST'))=='1.0.4')
stopifnot(as.character(getRversion())=='4.4.3')
lock <- read.csv(file.path(dirname(dirname(args[7])),'reference-r-packages.csv'),stringsAsFactors=FALSE)
for (i in seq_len(nrow(lock))) {
  package <- lock$Package[i]
  fixed <- normalizePath(file.path(args[7],package),winslash='/',mustWork=TRUE)
  # Read the file itself: Windows ACL probes can incorrectly report no access.
  actual <- read.dcf(file.path(fixed,'DESCRIPTION'),fields=c('Package','Version'))
  stopifnot(actual[1,'Package']==package,actual[1,'Version']==lock$Version[i])
  resolved <- normalizePath(find.package(package),winslash='/',mustWork=TRUE)
  stopifnot(tolower(resolved)==tolower(fixed))
}
train <- read.csv(args[1]);pred <- read.csv(args[2]);out <- args[3]
tp <- sf::st_as_sf(train,coords=c('x','y'),crs=32637)
pp <- sf::st_as_sf(pred,coords=c('x','y'),crs=32637)
# Coordinates are already in the chosen projected metric space. The fixed
# projected CRS below only selects Euclidean distances; no re-projection occurs.
set.seed(as.integer(args[6]))
res <- knndm(tp,predpoints=pp,k=as.integer(args[4]),maxp=as.numeric(args[5]),
             clustering='hierarchical',linkf='ward.D2',algorithm='brute')
fold <- rep(NA_integer_,nrow(train))
for(j in seq_along(res$indx_test)) fold[res$indx_test[[j]]] <- j-1L
stopifnot(!anyNA(fold),length(res$indx_test)==as.integer(args[4]))
write.csv(data.frame(fold=fold,Gj=res$Gj,Gjstar=res$Gjstar),file.path(out,'training.csv'),row.names=FALSE)
write.csv(data.frame(Gij=res$Gij),file.path(out,'prediction.csv'),row.names=FALSE)
write.csv(data.frame(W=res$W,q=res$q,CAST=as.character(packageVersion('CAST'))),file.path(out,'summary.csv'),row.names=FALSE)
