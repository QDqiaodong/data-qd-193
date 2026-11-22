IDENTITY=(1,0,0,1,0,0)

def compose(parent,local):
    a,b,c,d,x,y=parent;e,f,g,h,u,v=local
    return (a*e+c*f,b*e+d*f,a*g+c*h,b*g+d*h,a*u+c*v+x,b*u+d*v+y)

def point(matrix,xy):
    a,b,c,d,x,y=matrix;u,v=xy
    return (a*u+c*v+x,b*u+d*v+y)

def determinant(matrix):return matrix[0]*matrix[3]-matrix[1]*matrix[2]
