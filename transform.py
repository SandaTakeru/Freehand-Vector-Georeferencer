# -*- coding: utf-8 -*-
'''対応点（GCP）から変換行列を計算するモジュール。

NumPy で最小二乗フィットを行い、2x3 のアフィン行列 M を返す。
  x' = M[0,0]*x + M[0,1]*y + M[0,2]
  y' = M[1,0]*x + M[1,1]*y + M[1,2]

変換モードは 2 種類のみ:
  - 'helmert': 回転 + 平行移動（+ 任意の等倍）。lock_scale=True で scale=1 固定（剛体）
  - 'affine' : 回転 + 平行移動 + 拡大縮小 + 剪断（フル 6 パラメータ）

点数が足りない場合は段階的にフォールバックする:
  1 点 → 平行移動のみ / 2 点 → ヘルメルト相当 / 3 点以上 → 各モードの本来の自由度
'''

import numpy as np


def _identity():
    return np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])


def _translation(src, dst):
    # 平行移動のみ。対応点の重心差で決める
    t = (dst - src).mean(axis=0)
    return np.array([[1.0, 0.0, t[0]], [0.0, 1.0, t[1]]])


def _rigid(src, dst):
    # 等倍固定（剛体）: 直交プロクラステス法で最適回転を求める
    mu_s = src.mean(axis=0)
    mu_d = dst.mean(axis=0)
    S = src - mu_s
    D = dst - mu_d
    H = S.T @ D
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, d]) @ U.T
    t = mu_d - R @ mu_s
    return np.array([[R[0, 0], R[0, 1], t[0]], [R[1, 0], R[1, 1], t[1]]])


def _similarity(src, dst):
    # ヘルメルト（相似）: x' = a*x - b*y + tx, y' = b*x + a*y + ty を最小二乗で解く
    A = []
    B = []
    for (x, y), (X, Y) in zip(src, dst):
        A.append([x, -y, 1.0, 0.0])
        B.append(X)
        A.append([y, x, 0.0, 1.0])
        B.append(Y)
    p, _res, _rank, _sv = np.linalg.lstsq(np.array(A), np.array(B), rcond=None)
    a, b, tx, ty = p
    return np.array([[a, -b, tx], [b, a, ty]])


def _affine(src, dst):
    # アフィン: x' = a*x + b*y + c, y' = d*x + e*y + f を成分ごとに最小二乗で解く
    A = np.column_stack([src[:, 0], src[:, 1], np.ones(len(src))])
    px, _r1, _k1, _s1 = np.linalg.lstsq(A, dst[:, 0], rcond=None)
    py, _r2, _k2, _s2 = np.linalg.lstsq(A, dst[:, 1], rcond=None)
    return np.array([[px[0], px[1], px[2]], [py[0], py[1], py[2]]])


def compute_matrix(src, dst, mode='helmert', lock_scale=False):
    '''対応点から 2x3 変換行列を計算する。

    src, dst : Nx2 の配列（旧座標 / 新座標）
    '''
    src = np.asarray(src, dtype=float).reshape(-1, 2)
    dst = np.asarray(dst, dtype=float).reshape(-1, 2)
    n = len(src)
    if n == 0:
        return _identity()
    if n == 1:
        return _translation(src, dst)
    if mode == 'affine':
        if n >= 3:
            return _affine(src, dst)
        # 2 点ではアフィンは未決定なのでヘルメルトにフォールバック
        return _similarity(src, dst)
    # helmert
    if lock_scale:
        return _rigid(src, dst)
    return _similarity(src, dst)


def apply_matrix(M, pts):
    '''Nx2 の点群に 2x3 行列を適用して Nx2 を返す。'''
    pts = np.asarray(pts, dtype=float).reshape(-1, 2)
    return (M[:, :2] @ pts.T + M[:, 2:3]).T


def residuals(M, src, dst):
    '''各対応点の残差 dst - 変換後src を Nx2 で返す。'''
    src = np.asarray(src, dtype=float).reshape(-1, 2)
    dst = np.asarray(dst, dtype=float).reshape(-1, 2)
    return dst - apply_matrix(M, src)


def error_stats(M, src, dst):
    '''残差統計をまとめて返す。

    戻り値 dict:
      per_point : Nx3 (ex, ey, mag)
      rms       : 全体 RMS（mag ベース）
      std       : mag の標準偏差
    '''
    res = residuals(M, src, dst)
    if len(res) == 0:
        return {'per_point': np.zeros((0, 3)), 'rms': 0.0, 'std': 0.0}
    mag = np.sqrt(res[:, 0] ** 2 + res[:, 1] ** 2)
    per_point = np.column_stack([res[:, 0], res[:, 1], mag])
    rms = float(np.sqrt(np.mean(res[:, 0] ** 2 + res[:, 1] ** 2)))
    std = float(np.std(mag))
    return {'per_point': per_point, 'rms': rms, 'std': std}


def to_qtransform(M):
    '''2x3 行列を QTransform に変換する。

    QTransform(m11, m12, m21, m22, dx, dy) は
      x' = m11*x + m21*y + dx
      y' = m12*x + m22*y + dy
    なので、本モジュールの M とは下記の対応になる。
    '''
    from qgis.PyQt.QtGui import QTransform
    return QTransform(
        M[0, 0], M[1, 0],
        M[0, 1], M[1, 1],
        M[0, 2], M[1, 2],
    )
