from frame import poseRt
import time
import numpy as np
import g2o
import json

LOCAL_WINDOW = 20
#LOCAL_WINDOW = None

class Point(object):
  # A Point is a 3-D point in the world
  # Each Point is observed in multiple Frames

  def __init__(self, mapp, loc, color):
    self.pt = loc
    self.frames = []
    self.idxs = []
    self.color = np.copy(color)
    self.id = mapp.add_point(self)

  def homogeneous(self):
    return np.array([self.pt[0], self.pt[1], self.pt[2], 1.0])

  def orb(self):
    return [f.des[f.pts.index(self)] for f in self.frames]
  
  def delete(self):
    for f in self.frames:
      f.pts[f.pts.index(self)] = None
    del self

  def add_observation(self, frame, idx):
    frame.pts[idx] = self
    self.frames.append(frame)
    self.idxs.append(idx)

class Map(object):
  def __init__(self):
    self.frames = []
    self.points = []
    self.max_frame = 0
    self.max_point = 0

  def add_point(self, point):
    ret = self.max_point
    self.max_point += 1
    self.points.append(point)
    return ret

  def add_frame(self, frame):
    ret = self.max_frame
    self.max_frame += 1
    self.frames.append(frame)
    return ret

  # *** optimizer ***

  def optimize(self, local_window=LOCAL_WINDOW, fix_points=False, verbose=False):
    # create g2o optimizer
    opt = g2o.SparseOptimizer()
    solver = g2o.BlockSolverSE3(g2o.LinearSolverCholmodSE3())
    solver = g2o.OptimizationAlgorithmLevenberg(solver)
    opt.set_algorithm(solver)

    robust_kernel = g2o.RobustKernelHuber(np.sqrt(5.991))

    if local_window is None:
      local_frames = self.frames
    else:
      local_frames = self.frames[-local_window:]

    # add frames to graph
    for f in self.frames:
      pose = np.linalg.inv(f.pose)
      sbacam = g2o.SBACam(g2o.SE3Quat(pose[0:3, 0:3], pose[0:3, 3]))
      sbacam.set_cam(f.K[0][0], f.K[1][1], f.K[0][2], f.K[1][2], 1.0)

      v_se3 = g2o.VertexCam()
      v_se3.set_id(f.id)
      v_se3.set_estimate(sbacam)
      v_se3.set_fixed(f.id <= 1 or f not in local_frames)
      opt.add_vertex(v_se3)

    # add points to frames
    PT_ID_OFFSET = 0x10000
    for p in self.points:
      if not any([f in local_frames for f in p.frames]):
        continue

      pt = g2o.VertexSBAPointXYZ()
      pt.set_id(p.id + PT_ID_OFFSET)
      pt.set_estimate(p.pt[0:3])
      pt.set_marginalized(True)
      pt.set_fixed(fix_points)
      opt.add_vertex(pt)

      for f in p.frames:
        edge = g2o.EdgeProjectP2MC()
        edge.set_vertex(0, pt)
        edge.set_vertex(1, opt.vertex(f.id))
        uv = f.kpus[f.pts.index(p)]
        edge.set_measurement(uv)
        edge.set_information(np.eye(2))
        edge.set_robust_kernel(robust_kernel)
        opt.add_edge(edge)
        
    if verbose:
      opt.set_verbose(True)
    opt.initialize_optimization()
    opt.optimize(20)

    # put frames back
    for f in self.frames:
      est = opt.vertex(f.id).estimate()
      R = est.rotation().matrix()
      t = est.translation()
      f.pose = np.linalg.inv(poseRt(R, t))

    # put points back (and cull)
    if not fix_points:
      new_points = []
      for p in self.points:
        vert = opt.vertex(p.id + PT_ID_OFFSET)
        if vert is None:
          new_points.append(p)
          continue
        est = vert.estimate()

        # <= 3 match point that's old
        old_point = len(p.frames) <= 3 and p.frames[-1] not in local_frames

        # compute reprojection error
        errs = []
        for f in p.frames:
          uv = f.kpus[f.pts.index(p)]
          proj = np.dot(np.dot(f.K, f.pose[:3]),
                        np.array([est[0], est[1], est[2], 1.0]))
          proj = proj[0:2] / proj[2]
          errs.append(np.linalg.norm(proj-uv))

        # cull
        if old_point or np.mean(errs) > 5:
          p.delete()
          continue

        p.pt = np.array(est)
        new_points.append(p)
      print("Culled:   %d points" % (len(self.points) - len(new_points)))
      self.points = new_points

    return opt.active_chi2()

