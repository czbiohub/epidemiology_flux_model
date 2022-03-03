% unfolding  safeGraph fluxes for a specified set of flux matrices
% remove edges by chunks according to betweenness edge-centrality.
% find unfolded spectra, KL divergence, entropy and toll for the modified set. 
%
% Input: matrices in 'DATA/symmetricFlux
% Use: set_params, mload_flux_matrices, unfold_csaps, eval2ps (eig2spc), mk_banded, 
% mk_gamdiag, /CENT/plot_cent_toll
% Note: plots are generated by plotF2.m-plotF6.m and /CENT/plot_cent_toll.m

clear;
close all; 
set_params;          % parameters
mload_flux_matrices; % load flux matrices to workspace 
%% make zero diagonal
if strcmp(typDiag,'zero')
    for s=1:nsamp
        for n=1:N; F(n,n,s)=0;end
    end
end
%% make a banded matrix
if isBanded
    K=N/2;
    B=mk_banded(K,N);
else
    B=ones(N,N);
end
gamdiag=1;
if gamdiag
   F=mk_gamdiag(F); 
end

%% folder for figures
dir_fig='CENT'; 
if 0
    set_dir_fig;        % makes dirs for figures
    get_cent_rmv_links; % list of removed links according to the distance matrix
    mk_cent_nnz_figs;   % makes nnz figures and movie
end
load([dir_fig,'/','rmv_cent_toll.mat']);
%% evals and total toll
rng=[1:150,160:10:ndst];  % rng are selected values of 1<=n<=ndst    
if 0;get_cent_eval_omega; end
%% level spacing
omega=zeros(ndst,1);    % final toll statistics: [mean,std,min,max]
D=zeros(ndst,1);        % KL
nbins1=ceil(2*sqrt(N*nsamp));
nbins2=ceil(2*sqrt(N));
Esn=zeros(N,nsamp,ndst);
Tns=zeros(ndst,nsamp); 
qq=0;
KL=zeros(3,length(rng));
rng=[1:10:150,200:50:ndst];  % down-sampling
for n=rng
    qq=qq+1;
    fname=[dir_fig,'/','eval','/',num2str(n,'%05u'),'.mat'];
    load(fname,'Es','Ts');
    %Q=Es;figure(2); plotF2;
    %saveas(gcf,[dir_fig,'/','dos1','/',num2str(n,'%05u'),'.png']);
    warning('off');
    [ps1,s1,d1,n1,wdy1,pois1]=eig2spc(Es,5*nbins1,1);
    fprintf('%u\t%s\t%s%2.6f\t%s%2.6f\t%s%2.6f\n',...
        n,'raw:','sAvg=',d1,'ds=',mean(diff(s1)),'nrm=',n1); 
    %figure(3);plotF3;
    %saveas(gcf,[dir_fig,'/','raw','/',num2str(n,'%05u'),'.png']);
    [G,~,~]=unfold_csaps(Es',5*nbins1);
    [ps2,s2,d2,n2,wdy2,pois2]=eig2spc(G',nbins2,1);
    warning('on');
    fprintf('%u\t%s\t%s%2.6f\t%s%2.6f\t%s%2.6f\n',...
        n,'unf:','sAvg=',d2,'ds=',mean(diff(s2)),'nrm=',n2);
    %figure(4); plotF4;
    %saveas(gcf,[dir_fig,'/','dos2','/',num2str(n,'%05u'),'.png']);
    if smoothKL
        ps2=smooth(ps2,3);ps2=ps2.';
    end
    ps2(ps2<=0)=eps;
    D(n)=trapz(s2,ps2.*(log(ps2)+s2));   % KL(data|| poisson)=1-entropy(data)
    KL(1,qq)=D(n);
    KL1=D(n);
    %figure(5);plotF5;
    %saveas(gcf,[dir_fig,'/','unf','/',num2str(n,'%05u'),'.png']);
    omega(n)=mean(Ts);
    fprintf('\n%s\t%2.6f\t%s\t%2.6f\t\t','KL:',D(n),'Omega:',omega(n)); 
    Esn(:,:,n)=Es;
    Tns(n,:)=Ts;
    [KL(2,qq),p1,s1,q2,q3,d1,d2,nrm,nrm2,nrm3,Gs]=eval2ps(Es);
    KL2=KL(2,qq);
    fprintf('\n%2.6f\t%s\t%2.6f\t%2.6f\t%2.6f\n',d2,'KL:',KL1,KL2);
    figure(6);plotF6; 
    %saveas(gcf,[dir_fig,'/','unf','/',num2str(n,'%05u'),'.png']);
%     if n==2500; saveas(gcf,[dir_fig,'/','unf','/',num2str(n,'%05u'),'.svg']);end
%     if n==3000;saveas(gcf,[dir_fig,'/','unf','/',num2str(n,'%05u'),'.svg']);end
end
%save([dir_fig,'/','res_cent.mat'],'D','Tns','Esn','removedEdgeList','Nnzero','KL','-v7.3');
%save([dir_fig,'/','res_cent.mat'],'D','Tns','removedEdgeList','Nnzero','KL');
cd(dir_fig);plot_cent_toll;cd .. 